import json
import urllib.parse
import silopub.models


FAKE_SERVICE_NAME = 'fake'


class FakeSite(silopub.models.Site):
    __mapper_args__ = {'polymorphic_identity': FAKE_SERVICE_NAME}

    def __repr__(self):
        return 'FakeSite[username={}]'.format(self.site_id)


class FakeResponse:
    def __init__(self, text='', status_code=200, url=None):
        self.text = text
        self.status_code = status_code
        self.content = text and bytes(text, 'utf8')
        self.url = url
        self.headers = {'content-type': 'text/html'}

    def json(self):
        return json.loads(self.text)

    def __repr__(self):
        return 'FakeResponse(status={}, text={}, url={})'.format(
            self.status_code, self.text, self.url)


def assertUrlsMatch(expected, actual):
    p1 = urllib.parse.urlparse(expected)
    p2 = urllib.parse.urlparse(actual)
    assert p1.scheme == p2.scheme
    assert p1.netloc == p2.netloc
    assert p1.path == p2.path
    assert urllib.parse.parse_qs(p1.query) == urllib.parse.parse_qs(p2.query)
