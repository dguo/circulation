import logging
from nose.tools import set_trace
from lxml import etree
from urlparse import urljoin
from urllib import urlencode
import datetime
import requests

from core.util.xmlparser import XMLParser
from authenticator import Authenticator
from config import Configuration
import os
import re
from core.model import (
    get_one,
    get_one_or_create,
    Patron,
)

class MilleniumPatronAPI(Authenticator, XMLParser):

    TYPE = Authenticator.BASIC_AUTH
    NAME = "Millenium"

    RECORD_NUMBER_FIELD = 'RECORD #[p81]'
    PATRON_TYPE_FIELD = 'P TYPE[p47]'
    EXPIRATION_FIELD = 'EXP DATE[p43]'
    BARCODE_FIELD = 'P BARCODE[pb]'
    USERNAME_FIELD = 'ALT ID[pu]'
    FINES_FIELD = 'MONEY OWED[p96]'
    EXPIRATION_DATE_FORMAT = '%m-%d-%y'

    MULTIVALUE_FIELDS = set(['NOTE[px]'])

    REPORTED_LOST = re.compile("^CARD([0-9]{14})REPORTEDLOST")

    # How long we should go before syncing our internal Patron record
    # with Millenium.
    MAX_STALE_TIME = datetime.timedelta(hours=12)

    log = logging.getLogger("Millenium Patron API")

    def __init__(self, root):
        if not root.endswith('/'):
            root = root + "/"
        self.root = root
        self.parser = etree.HTMLParser()

    @classmethod
    def from_config(cls):
        config = Configuration.integration(
            Configuration.MILLENIUM_INTEGRATION, required=True)
        host = config.get(Configuration.URL)
        if not host:
            cls.log.info("No Millenium Patron client configured.")
            return None
        return cls(host)            

    def request(self, url):
        return requests.get(url)

    def _extract_text_nodes(self, content):
        tree = etree.fromstring(content, self.parser)
        for i in tree.xpath("(descendant::text() | following::text())"):
            i = i.strip()
            if i:
                yield i.split('=', 1)

    def dump(self, barcode):
        path = "%(barcode)s/dump" % dict(barcode=barcode)
        url = self.root + path
        print url
        response = self.request(url)
        if response.status_code != 200:
            msg = "Got unexpected response code %d. Content: %s" % (
                response.status_code, response.content
            )
            raise Exception(msg)
        d = dict()
        for k, v in self._extract_text_nodes(response.content):
            if k in self.MULTIVALUE_FIELDS:
                d.setdefault(k, []).append(v)
            else:
                d[k] = v
        return d

    def pintest(self, barcode, pin):
        path = "%(barcode)s/%(pin)s/pintest" % dict(barcode=barcode, pin=pin)
        url = self.root + path
        print url
        response = self.request(url)
        if response.status_code != 200:
            msg = "Got unexpected response code %d. Content: %s" % (
                response.status_code, response.content
            )
            raise Exception(msg)
        data = dict(self._extract_text_nodes(response.content))
        if data.get('RETCOD') == '0':
            return True
        return False

    def update_patron(self, patron, identifier, dump=None):
        """Update a Patron record with information from a data dump."""
        if not dump:
            dump = self.dump(identifier)
        patron.authorization_identifier = dump.get(self.BARCODE_FIELD, None)
        patron.username = dump.get(self.USERNAME_FIELD, None)
        patron.fines = dump.get(self.FINES_FIELD, None)
        patron._external_type = dump.get(self.PATRON_TYPE_FIELD, None)
        expires = dump.get(self.EXPIRATION_FIELD, None)
        expires = datetime.datetime.strptime(
            expires, self.EXPIRATION_DATE_FORMAT).date()
        patron.authorization_expires = expires

    def patron_info(self, identifier):
        """Get patron information from the ILS."""
        dump = self.dump(identifier)
        return dict(
            barcode = dump.get(self.BARCODE_FIELD),
            username = dump.get(self.USERNAME_FIELD),
        )
        
    def authenticated_patron(self, db, header):
        identifier = header.get('username')
        password = header.get('password')

        # If they fail basic validation, there is no authenticated patron.
        if not self.server_side_validation(identifier, password):
            return None

        # If they fail a PIN test, it's very simple: there is 
        # no authenticated patron.
        if not self.pintest(identifier, password):
            return None

        now = datetime.datetime.utcnow()

        # Now it gets more complicated. There is *some* authenticated
        # patron, but it might not correspond to a Patron in our
        # database, and if it does, that Patron's
        # authorization_identifier might be different from the
        # identifier passed in to this method.

        # Let's start with a simple lookup based on identifier.
        kwargs = {Patron.authorization_identifier.name: identifier}
        patron = get_one(db, Patron, **kwargs)

        if not patron:
            # The patron might have used a username instead of a barcode.
            kwargs = {Patron.username.name: identifier}
            patron = get_one(db, Patron, *kwargs)

        __transaction = db.begin_nested()
        if patron:
            # We found them!
            if (not patron.last_external_sync
                or (now - patron.last_external_sync) > self.MAX_STALE_TIME):
                # Sync our internal Patron record with what the API
                # says.
                self.update_patron(patron, identifier)
                patron.last_external_sync = now
            __transaction.commit()
            return patron

        # We didn't find them. Now the question is: _why_ doesn't this
        # patron show up in our database? Have we never seen them
        # before, has their authorization identifier (barcode)
        # changed, or do they not exist in Millenium either?
        dump = self.dump(identifier)
        if dump.get('ERRNUM') in ('1', '2'):
            # The patron does not exist in Millenium. This is a bad
            # barcode. How we passed the PIN test is a mystery, but
            # ours not to reason why. There is no authenticated
            # patron.

            # TODO: EXCEPT, this might be a test patron dynamically
            # created by the test code.
            if len(identifier) != 14:
                print "Creating test patron!"
                patron, is_new = get_one_or_create(
                    db, Patron, external_identifier=identifier,
                )
                patron.authorization_identifier = identifier
                __transaction.commit()
                return patron
            __transaction.commit()
            return None

        # If we've gotten this far, the patron does exist in
        # Millenium.
        permanent_id = dump.get(self.RECORD_NUMBER_FIELD)
        if not permanent_id:
            # We have no reliable way of identifying this patron.
            # This should never happen, but if it does, we can't
            # create a Patron record.
            __transaction.commit()
            return None
        # Look up the Patron record by the permanent record ID. If
        # there is no such patron, we've never seen them
        # before--create a new Patron record for them.
        #
        # If there is such a patron, their barcode has changed,
        # probably because their old barcode was reported lost. We
        # will update their barcode in the next step.
        patron, is_new = get_one_or_create(
            db, Patron, external_identifier=permanent_id)

        # Update the new/out-of-date Patron record with information
        # from the data dump.
        self.update_patron(patron, identifier, dump)
        __transaction.commit()
        return patron

class DummyMilleniumPatronAPI(MilleniumPatronAPI):


    # This user's card has expired.
    user1 = { 'PATRN NAME[pn]' : "SHELDON, ALICE",
              'RECORD #[p81]' : "12345",
              'P BARCODE[pb]' : "0",
              'ALT ID[pu]' : "alice",
              'EXP DATE[p43]' : "04-01-05"
    }
    
    # This user's card still has ten days on it.
    the_future = datetime.datetime.utcnow() + datetime.timedelta(days=10)
    user2 = { 'PATRN NAME[pn]' : "HEINLEIN, BOB",
              'RECORD #[p81]' : "67890",
              'P BARCODE[pb]' : "5",
              'MONEY OWED[p96]' : "$1.00",
              'EXP DATE[p43]' : the_future.strftime("%m-%d-%y")
    }

    users = [user1, user2]

    def __init__(self):
        pass

    def pintest(self, barcode, pin):
        """A barcode that's 14 digits long is treated as valid,
        no matter which PIN is used.

        That's so real barcode/PIN combos can be passed through to
        third parties.

        Otherwise, valid test PIN is the first character of the barcode
        repeated four times.

        """
        u = self.dump(barcode)
        if 'ERRNUM' in u:
            return False
        return len(barcode) == 14 or pin == barcode[0] * 4

    def dump(self, barcode):
        # We have a couple custom barcodes.
        for u in self.users:
            if u['P BARCODE[pb]'] == barcode:
                return u
            if 'ALT ID[pu]' in u and u['ALT ID[pu]'] == barcode:
                return u
                
        # A barcode that starts with '404' does not exist.
        if barcode.startswith('404'):
            return dict(ERRNUM='1', ERRMSG="Requested record not found")

        # A barcode that starts with '410' has expired.
        if barcode.startswith('404'):
            u = dict(self.user1)
            u['RECORD #[p81]'] = "410" + barcode
            return 

        # Any other barcode is fine.
        u = dict(self.user2)
        u['P BARCODE[pb]'] = barcode
        u['RECORD #[p81]'] = "200" + barcode
        return u

AuthenticationAPI = MilleniumPatronAPI
