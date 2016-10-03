from silopub import util
from silopub.ext import db
from silopub.models import Account, Tumblr
from flask import Blueprint, current_app, redirect, url_for, request, flash
from flask import session
from requests_oauthlib import OAuth1Session, OAuth1
import html
import requests
import sys


REQUEST_TOKEN_URL = 'https://www.tumblr.com/oauth/request_token'
AUTHORIZE_URL = 'https://www.tumblr.com/oauth/authorize'
ACCESS_TOKEN_URL = 'https://www.tumblr.com/oauth/access_token'
USER_INFO_URL = 'https://api.tumblr.com/v2/user/info'
CREATE_POST_URL = 'https://api.tumblr.com/v2/blog/{}/post'
FETCH_POST_URL = 'https://api.tumblr.com/v2/blog/{}/posts'
SERVICE_NAME = 'tumblr'

tumblr = Blueprint('tumblr', __name__)


@tumblr.route('/tumblr/authorize', methods=['POST'])
def authorize():
    try:
        callback_uri = url_for('.callback', _external=True)
        return redirect(get_authorize_url(callback_uri))
    except:
        current_app.logger.exception('Starting Tumblr authorization')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


def get_authorize_url(callback_uri, **kwargs):
    oauth = OAuth1Session(
        client_key=current_app.config['TUMBLR_CLIENT_KEY'],
        client_secret=current_app.config['TUMBLR_CLIENT_SECRET'],
        callback_uri=callback_uri)
    r = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    session['oauth_token_secret'] = r.get('oauth_token_secret')
    return oauth.authorization_url(AUTHORIZE_URL)


@tumblr.route('/tumblr/callback')
def callback():
    try:
        callback_uri = url_for('.callback', _external=True)
        result = process_callback(callback_uri)
        if 'error' in result:
            flash(result['error'], category='danger')
            return redirect(url_for('views.index'))
        account = result['account']
        flash('Authorized {}: {}'.format(account.username, ', '.join(
            s.domain for s in account.sites)))
        return redirect(url_for('views.setup_account', service=SERVICE_NAME,
                                user_id=account.user_id))

    except:
        current_app.logger.exception('During Tumblr authorization callback')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


def process_callback(callback_uri):
    verifier = request.args.get('oauth_verifier')
    request_token = request.args.get('oauth_token')
    if not verifier or not request_token:
        # user declined
        return {'error': 'Tumblr authorization declined'}

    request_token_secret = session.get('oauth_token_secret')
    oauth = OAuth1Session(
        client_key=current_app.config['TUMBLR_CLIENT_KEY'],
        client_secret=current_app.config['TUMBLR_CLIENT_SECRET'],
        resource_owner_key=request_token,
        resource_owner_secret=request_token_secret)
    oauth.parse_authorization_response(request.url)
    # get the access token and secret
    r = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    token = r.get('oauth_token')
    secret = r.get('oauth_token_secret')

    info_resp = oauth.get(USER_INFO_URL).json()
    user_info = info_resp.get('response', {}).get('user')
    user_id = username = user_info.get('name')

    account = Account.query.filter_by(
        service='tumblr', user_id=user_id).first()

    if not account:
        account = Account(service='tumblr', user_id=user_id)
        db.session.add(account)

    account.username = username
    account.user_info = user_info
    account.token = token
    account.token_secret = secret

    sites = []
    for blog in user_info.get('blogs', []):
        sites.append(Tumblr(
            url=blog.get('url'),
            domain=util.domain_for_url(blog.get('url')),
            site_id=blog.get('name'),
            site_info=blog))
    account.update_sites(sites)

    db.session.commit()
    util.set_authed(account.sites)
    return {'account': account}


def publish(site):
    auth = OAuth1(
        client_key=current_app.config['TUMBLR_CLIENT_KEY'],
        client_secret=current_app.config['TUMBLR_CLIENT_SECRET'],
        resource_owner_key=site.account.token,
        resource_owner_secret=site.account.token_secret)

    create_post_url = CREATE_POST_URL.format(site.domain)
    photo_url = util.get_first(util.get_possible_array_value(request.form, 'photo'))
    photo_file = util.get_first(util.get_possible_array_value(request.files, 'photo'))

    if photo_url:
        data = util.trim_nulls({
            'type': 'photo',
            'slug': request.form.get('slug'),
            'caption': request.form.get('content[html]') or
            request.form.get('content') or request.form.get('name') or
            request.form.get('summary'),
            'source': photo_url
        })
        r = requests.post(create_post_url, data=data, auth=auth)

    elif photo_file:
        # tumblr signs multipart in a weird way. first sign the request as if
        # it's application/x-www-form-urlencoded, then recreate the request as
        # multipart but use the signed headers from before. Mostly cribbed from
        # https://github.com/tumblr/pytumblr/blob/\
        # 20e7e38ba6f0734335deee64d4cae45fa8a2ce90/pytumblr/request.py#L101

        # The API documentation and some of the code samples gave me the
        # impression that you could also send files just as part of the
        # form-encoded data but I couldnit make it work
        # https://www.tumblr.com/docs/en/api/v2#pphoto-posts
        # https://gist.github.com/codingjester/1649885#file-upload-php-L56
        data = util.trim_nulls({
            'type': 'photo',
            'slug': request.form.get('slug'),
            'caption': request.form.get('content[html]') or
            request.form.get('content') or request.form.get('name') or
            request.form.get('summary'),
        })
        fake_req = requests.Request('POST', create_post_url, data=data)
        fake_req = fake_req.prepare()
        auth(fake_req)

        real_headers = dict(fake_req.headers)

        # manually strip these, requests will recalculate them for us
        del real_headers['Content-Type']
        del real_headers['Content-Length']

        current_app.logger.info(
            'uploading photo to tumblr %s, headers=%r',
            create_post_url, real_headers)
        r = requests.post(create_post_url, data=data, files={
            'data': photo_file,
        }, headers=real_headers)

    else:
        data = util.trim_nulls({
            # one of: text, photo, quote, link, chat, audio, video
            'type': 'text',
            'slug': request.form.get('slug'),
            'title': request.form.get('name'),
            'body': util.get_complex_content(request.form),
        })
        current_app.logger.info(
            'posting to tumblr %s, data=%r', create_post_url, data)
        r = requests.post(create_post_url, data=data, auth=auth)

    current_app.logger.info(
        'response from tumblr %r, data=%r, headers=%r',
        r, r.content, r.headers)

    if r.status_code // 100 != 2:
        current_app.logger.warn(
            'Tumblr publish failed with response %s', r.text)
        return util.wrap_silo_error_response(r)

    location = None
    if 'Location' in r.headers:
        location = r.headers['Location']
    else:
        # only get back the id, look up the url
        post_id = r.json().get('response').get('id')
        r = requests.get(FETCH_POST_URL.format(site.domain), params={
            'api_key': current_app.config['TUMBLR_CLIENT_KEY'],
            'id': post_id,
        })
        if r.status_code // 100 == 2:
            posts = r.json().get('response', {}).get('posts', [])
            if posts:
                location = posts[0].get('post_url')

    return util.make_publish_success_response(location)
