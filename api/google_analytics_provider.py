from config import Configuration
import uuid
import urllib
from core.util.http import HTTP

class GoogleAnalyticsProvider(object):
    INTEGRATION_NAME = "Google Analytics"
    
    @classmethod
    def from_config(cls, config):
        tracking_id = config[Configuration.INTEGRATIONS][cls.INTEGRATION_NAME]['tracking_id']
        return cls(tracking_id)

    def __init__(self, tracking_id):
        self.tracking_id = tracking_id

    def format_range(self, r):
        if not r or not r.lower:
            return None
        min = r.lower if r.lower_inc else r.lower + 1
        if r.upper:
            max = r.upper + 1 if r.upper_inc else r.upper
            ",".join(range(min, max))
        else:
            return str(min)

    def collect_event(self, _db, license_pool, event_type, time, **kwargs):
        client_id = uuid.uuid4()
        work = license_pool.work
        edition = license_pool.presentation_edition
        params = urllib.urlencode({
            'v': 1,
            'tid': self.tracking_id,
            'cid': client_id,
            't': 'event',
            'ec': 'circulation',
            'ea': event_type,
            'cd1': license_pool.identifier.identifier,
            'cd2': edition.title,
            'cd3': edition.author,
            'cd4': "fiction" if work.fiction else "nonfiction",
            'cd5': work.audience,
            'cd6': edition.publisher,
            'cd7': edition.language,
            'cd8': self.format_range(work.target_age),
            'cd9': time
        })
        self.post("http://www.google-analytics.com/collect", params)

    def post(self, url, params):
        response = HTTP.post_with_timeout(url, params)
        
Provider = GoogleAnalyticsProvider