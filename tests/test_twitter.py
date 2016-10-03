from flask import request, session
from silopub import twitter
from silopub.models import Account, Twitter
from silopub.testutil import FakeResponse, assertUrlsMatch
from werkzeug.datastructures import MultiDict, FileStorage
import os
import pytest
import json

CALLBACK_URI = 'http://localhost/callback'


@pytest.fixture
def site(db, account):
    site = Twitter(
        url='https://twitter.com/fakeuser',
        domain='twitter.com/fakeuser', site_id='fakeuser',
        account=account)
    db.session.add(site)
    db.session.commit()
    return site


@pytest.fixture
def account(db):
    account = Account(
        service='twitter', username='fakeuser', user_id='101010',
        token='123', token_secret='456')
    db.session.add(account)
    db.session.commit()
    return account


def test_get_authorize_url(app, mocker):
    post = mocker.patch('requests.Session.post')
    post.return_value = FakeResponse(
        'oauth_token=123&oauth_token_secret=456')
    auth_url = twitter.get_authorize_url(CALLBACK_URI)
    assertUrlsMatch(
        'https://api.twitter.com/oauth/authorize?force_login=true&oauth_token=123',
        auth_url)
    post.assert_called_once_with(twitter.REQUEST_TOKEN_URL)


def test_process_callback(app, mocker):
    getter = mocker.patch('requests.get')
    fetch_access_token = mocker.patch('requests_oauthlib.OAuth1Session.fetch_access_token')
    session['oauth_token_secret'] = '456'
    request.url = '/callback?oauth_token=123&oauth_verifier=789'
    request.args = {'oauth_token': '123', 'oauth_verifier': '789'}

    fetch_access_token.return_value = {'oauth_token': '123',
                                       'oauth_token_secret': '456'}

    getter.return_value = FakeResponse(json.dumps({
        'id_str': '101010',
        'screen_name': 'fakeuser',
        'extra_info': 'Hi',
    }))

    result = twitter.process_callback(CALLBACK_URI)

    assert 'account' in result
    account = result['account']
    assert '123' == account.token
    assert '456' == account.token_secret
    assert '101010' == account.user_id
    assert 'fakeuser' == account.username

    fetch_access_token.assert_called_once_with(
        twitter.ACCESS_TOKEN_URL)


def test_proxy_homepage(app, client):
    r = client.get('/twitter.com/fakeuser')
    rtext = r.get_data(as_text=True)

    assert(200 == r.status_code)
    assert(
        '<link rel="authorization_endpoint" href="http://localhost/indieauth"' in rtext)
    assert(
        '<link rel="me" href="https://twitter.com/fakeuser"' in rtext)


def test_publish_blank(app, site, mocker):
    poster = mocker.patch('requests.post')
    poster.return_value = FakeResponse(json.dumps({
        'error': 'Missing required parameter: status',
    }), 400)
    resp = twitter.publish(site)
    assert(400 == resp.status_code)
    assert('Missing required' in resp.get_data(as_text=True))


def test_publish_like(app, site, mocker):
    poster = mocker.patch('requests.post')
    request.form = MultiDict({
        'like-of': 'https://twitter.com/jack/status/20',
    })
    poster.return_value = FakeResponse(json.dumps({
        'user': {'screen_name': 'jack'},
        'id_str': '20',
    }))
    resp = twitter.publish(site)
    assert(201 == resp.status_code)
    assert('https://twitter.com/jack/status/20' == resp.headers['location'])
    poster.assert_called_once_with(twitter.FAVE_STATUS_URL, data={
        'id': '20',
    }, auth=mocker.ANY)


def test_publish_retweet(app, site, mocker):
    poster = mocker.patch('requests.post')
    request.form = MultiDict({
        'repost-of': 'https://twitter.com/mallelis/status/668573590170828802',
    })
    poster.return_value = FakeResponse(json.dumps({
        'user': {'screen_name': 'jenny'},
        'id_str': '8675309',
    }))
    resp = twitter.publish(site)
    assert(201 == resp.status_code)
    assert('https://twitter.com/jenny/status/8675309' == resp.headers['location'])
    poster.assert_called_once_with(
        twitter.RETWEET_STATUS_URL.format('668573590170828802'),
        auth=mocker.ANY)


def test_publish_tweet(app, site, mocker):
    poster = mocker.patch('requests.post')
    request.form = MultiDict({
        'content': 'You shall not pass by reference!',
        'url': 'https://foo.com/bar',
    })
    request.files = {}

    poster.return_value = FakeResponse(json.dumps({
        'user': {'screen_name': 'cppgandalf'},
        'id_str': '9899100',
    }))
    resp = twitter.publish(site)
    assert(201 == resp.status_code)
    assert('https://twitter.com/cppgandalf/status/9899100' == resp.headers['location'])
    poster.assert_called_once_with(
        twitter.CREATE_STATUS_URL, data={
            'status': 'You shall not pass by reference!',
        },
        auth=mocker.ANY)


def test_publish_media(app, site, mocker):
    poster = mocker.patch('requests.post')
    request.form = MultiDict({
        # no longer needs to be shortened now that twitter does not count the
        # photo against us
        'content': 'schon nach 40 minuten fast 20 mal so viele leser per #instantarticles gehabt, wie in 6 monaten per #amphtml-artikeln.',
    })
    request.files = MultiDict({
        'photo': FileStorage(
            open(os.path.dirname(__file__) + '/../silopub/static/dish256.png'),
            'dish256.png'),
    })

    poster.side_effect = [
        FakeResponse(json.dumps({
            'media_id_string': '2112',
        })),
        FakeResponse(json.dumps({
            'user': {'screen_name': 'fakeuser'},
            'id_str': '0123456789',
        })),
    ]

    resp = twitter.publish(site)
    assert 201 == resp.status_code
    assert 'https://twitter.com/fakeuser/status/0123456789' == resp.headers['location']

    poster.assert_has_calls([
        mocker.call(twitter.UPLOAD_MEDIA_URL, files={
            'media': ('dish256.png', mocker.ANY, mocker.ANY),
        }, auth=mocker.ANY),
        mocker.call(twitter.CREATE_STATUS_URL, data={
            'status': 'schon nach 40 minuten fast 20 mal so viele leser per #instantarticles gehabt, wie in 6 monaten per #amphtml-artikeln.',
            'media_ids': '2112',
        }, auth=mocker.ANY)
    ])


def test_publish_reply(site, mocker):
    poster = mocker.patch('requests.post')
    request.form = MultiDict({
        'in-reply-to': 'https://twitter.com/mashable/status/668134813325508609',
        'content': 'Speak, friend, and enter',
        'url': 'http://bar.co.uk/bat',
    })
    request.files = {}
    poster.return_value = FakeResponse(json.dumps({
        'user': {'screen_name': 'moriafan'},
        'id_str': '234567',
    }))
    resp = twitter.publish(site)
    assert 201 == resp.status_code
    assert 'https://twitter.com/moriafan/status/234567' == resp.headers['location']
    poster.assert_called_once_with(
        twitter.CREATE_STATUS_URL, data={
            'status': '@mashable Speak, friend, and enter',
            'in_reply_to_status_id': '668134813325508609',
        },
        auth=mocker.ANY)
