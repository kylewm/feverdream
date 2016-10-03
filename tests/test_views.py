from silopub.testutil import FakeSite, FAKE_SERVICE_NAME, assertUrlsMatch
from silopub.models import Account
from urllib.parse import urlencode
import re


def test_index(client):
    resp = client.get('/')
    resp_text = resp.get_data(as_text=True)
    assert 200 == resp.status_code
    assert 'Micropub clients post to silo.pub' in resp_text


def test_about(client):
    resp = client.get('/about')
    resp_text = resp.get_data(as_text=True)
    assert 200 == resp.status_code
    assert 'Micropub for Hosted Blogs' in resp_text


def test_setup_account_with_multiple_sites(db, client):
    site1 = FakeSite(url='https://fake1.example.com',
                     domain='fake1.example.com')
    site2 = FakeSite(url='https://fake2.example.com',
                     domain='fake2.example.com')

    acct = Account(service=FAKE_SERVICE_NAME,
                   user_id='1234',
                   username='fakeuser',
                   sites=[site1, site2])

    db.session.add(acct)
    db.session.commit()

    resp = client.get('/setup/account/', query_string={
        'service': FAKE_SERVICE_NAME,
        'user_id': '1234',
    })
    resp_text = resp.get_data(as_text=True)

    assert 200 == resp.status_code
    assert 'multiple sites for the same account' in resp_text


def test_setup_account_with_one_site(db, client):
    site1 = FakeSite(url='https://fake1.example.com',
                     domain='fake1.example.com')
    acct = Account(service=FAKE_SERVICE_NAME,
                   user_id='1234',
                   username='fakeuser',
                   sites=[site1])

    db.session.add(acct)
    db.session.commit()

    resp = client.get('/setup/account/', query_string={
        'service': FAKE_SERVICE_NAME,
        'user_id': '1234',
    })

    assert 302 == resp.status_code
    assertUrlsMatch(
        'http://localhost/setup/site/?service=fake&domain=fake1.example.com',
        resp.headers['location'])


def test_setup_site(db, client):
    site1 = FakeSite(url='https://fake1.example.com',
                     domain='good.example.com')
    site2 = FakeSite(url='https://fake2.example.com',
                     domain='bad.example.com')

    acct = Account(service=FAKE_SERVICE_NAME,
                   username='fakeuser',
                   sites=[site1, site2])

    db.session.add(acct)
    db.session.commit()

    params = {
        'service': FAKE_SERVICE_NAME,
        'domain': 'good.example.com',
    }
    r = client.get('/setup/site/', query_string=params)

    assert 302 == r.status_code
    assertUrlsMatch(
        'http://localhost/setup/micropub/?' + urlencode(params),
        r.headers['location'])

    r = client.get('/setup/micropub/', query_string=params)
    assert 200 == r.status_code

    rtext = re.sub(r'\s+', ' ', r.get_data(as_text=True))
    assert "you've authorized silo.pub to publish to good.example.com" in rtext
