import html
import requests
import sys
import re
import urllib.parse

from flask import Blueprint, current_app, redirect, url_for, request, flash
from flask import session, abort
from requests_oauthlib import OAuth1Session, OAuth1
from silopub import util
from silopub.ext import db
from silopub.models import Account, Flickr
from oauthlib.oauth1 import SIGNATURE_TYPE_BODY


SERVICE_NAME = 'flickr'

REQUEST_TOKEN_URL = 'https://www.flickr.com/services/oauth/request_token'
AUTHORIZE_URL = 'https://www.flickr.com/services/oauth/authorize'
AUTHENTICATE_URL = 'https://www.flickr.com/services/oauth/authenticate'
ACCESS_TOKEN_URL = 'https://www.flickr.com/services/oauth/access_token'
API_URL = 'https://api.flickr.com/services/rest'
UPLOAD_URL = 'https://up.flickr.com/services/upload'

FLICKR_PHOTO_RE = re.compile(
    r'https?://(?:www\.)?flickr.com/photos/([\w@]+)/(\d+)/?')
FLICKR_PERSON_RE = re.compile(
    r'https?://(?:www\.|)?flickr.com/(?:photos|people)/([\w@]+)/?')


flickr = Blueprint('flickr', __name__)


@flickr.route('/flickr.com/<nsid>')
def proxy_homepage(nsid):
    current_app.logger.debug('constructing proxy homepage with nsid: %s', nsid)
    account = Account.query.filter_by(
        service=SERVICE_NAME, user_id=nsid).first()

    if not account:
        abort(404)

    info = (account.user_info or {}).get('person', {})
    iconserver = info.get('iconserver')
    iconfarm = info.get('iconfarm')
    iconnsid = info.get('nsid')

    if iconserver and iconserver != 0:
        photo = 'http://farm{}.staticflickr.com/{}/buddyicons/{}.jpg'.format(
            iconfarm, iconserver, iconnsid)
    else:
        photo = 'https://www.flickr.com/images/buddyicon.gif'

    return util.render_proxy_homepage(
        user_name=account.username,
        user_url=account.sites[0].url,
        user_photo=photo,
        service_name='Flickr',
        service_url='https://www.flickr.com/',
        service_photo='https://s.yimg.com/pw/favicon.ico')


@flickr.route('/flickr/authorize', methods=['POST'])
def authorize():
    try:
        callback_uri = url_for('.callback', _external=True)
        return redirect(get_authorize_url(callback_uri))
    except:
        current_app.logger.exception('Starting Flickr authorization')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


@flickr.route('/flickr/callback')
def callback():
    try:
        callback_uri = url_for('.callback', _external=True)
        result = process_callback(callback_uri)
        if 'error' in result:
            flash(result['error'], category='danger')
            return redirect(url_for('views.index'))

        account = result['account']
        return redirect(url_for('views.setup_account', service=SERVICE_NAME,
                                user_id=account.user_id))

    except:
        current_app.logger.exception('During Flickr authorization callback')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


def get_authorize_url(callback_uri, me=None, **kwargs):
    oauth = OAuth1Session(
        client_key=current_app.config['FLICKR_CLIENT_KEY'],
        client_secret=current_app.config['FLICKR_CLIENT_SECRET'],
        callback_uri=callback_uri)
    r = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    session['oauth_token_secret'] = r.get('oauth_token_secret')
    return oauth.authorization_url(AUTHORIZE_URL, perms='write')


def process_callback(callback_uri):
    verifier = request.args.get('oauth_verifier')
    request_token = request.args.get('oauth_token')
    if not verifier or not request_token:
        # user declined
        return {'error': 'Flickr authorization declined'}

    request_token_secret = session.get('oauth_token_secret')
    oauth = OAuth1Session(
        client_key=current_app.config['FLICKR_CLIENT_KEY'],
        client_secret=current_app.config['FLICKR_CLIENT_SECRET'],
        resource_owner_key=request_token,
        resource_owner_secret=request_token_secret)
    oauth.parse_authorization_response(request.url)
    # get the access token and secret
    r = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    current_app.logger.debug('response from access token: %r', r)

    token = r.get('oauth_token')
    secret = r.get('oauth_token_secret')
    user_id = r.get('user_nsid')
    username = r.get('fullname')

    r = call_api_method('GET', 'flickr.people.getInfo', {
        'user_id': user_id
    }, token, secret)
    user_info = r.json()

    account = Account.query.filter_by(
        service=SERVICE_NAME, user_id=user_id).first()

    if not account:
        account = Account(service=SERVICE_NAME, user_id=user_id)
        db.session.add(account)

    account.username = username
    account.user_info = user_info
    account.token = token
    account.token_secret = secret

    account.update_sites([Flickr(
        url='https://flickr.com/{}'.format(account.user_id),
        domain='flickr.com/{}'.format(account.user_id),
        site_id=account.user_id)])

    db.session.commit()
    flash('Authorized {}: {}'.format(account.username, ', '.join(
        s.domain for s in account.sites)))
    util.set_authed(account.sites)
    return {'account': account}


def call_api_method(http_method, flickr_method, params,
                    token=None, secret=None, site=None):
    auth = OAuth1(
        client_key=current_app.config['FLICKR_CLIENT_KEY'],
        client_secret=current_app.config['FLICKR_CLIENT_SECRET'],
        resource_owner_key=token or site.token or site.account.token,
        resource_owner_secret=secret or site.token_secret or
        site.account.token_secret)

    full_params = {
        'nojsoncallback': 1,
        'format': 'json',
        'method': flickr_method,
    }
    full_params.update(params)
    return requests.request(http_method, API_URL, params=full_params,
                            auth=auth)


def upload(params, photo_file, token=None, secret=None, site=None):
    auth = OAuth1(
        client_key=current_app.config['FLICKR_CLIENT_KEY'],
        client_secret=current_app.config['FLICKR_CLIENT_SECRET'],
        resource_owner_key=token or site.token or site.account.token,
        resource_owner_secret=secret or site.token_secret or
        site.account.token_secret,
        signature_type=SIGNATURE_TYPE_BODY)

    # create a request without files for signing
    faux_req = requests.Request(
        'POST', UPLOAD_URL, data=params, auth=auth).prepare()
    # parse the signed parameters back out of the body
    data = urllib.parse.parse_qsl(faux_req.body)

    # and use them in the real request
    current_app.logger.debug('uploading with data: %s', data)
    resp = requests.post(UPLOAD_URL, data=data, files={
        'photo': photo_file,
    })
    current_app.logger.debug('upload response: %s, %s', resp, resp.text)
    return resp


def interpret_upload_response(resp):
    m = re.search('<rsp stat="(\w+)">', resp.text, re.DOTALL)
    if not m:
        return (None, 'Expected response with <rsp stat="...">. '
                'Got: %s' % resp.text)

    stat = m.group(1)
    if stat == 'fail':
        m = re.search('<err code="(\d+)" msg="([\w ]+)" />',
                      resp.text, re.DOTALL)
        if not m:
            return (None, 'Expected response with <err code="..." msg=".." />.'
                    ' Got: %s' % resp.text)
        return (None, 'Upstream error %d: %s' % (int(m.group(1)), m.group(2)))

    m = re.search('<photoid>(\d+)</photoid>', resp.text, re.DOTALL)
    if not m:
        return (None, 'Expected response with <photoid>...</photoid>. '
                'Got: %s' % resp.text)

    return m.group(1), None


def publish(site):
    def get_photo_id(original):
        """Based on an original URL, find the Flickr syndicated URL and
        extract the photo ID

        Returns a tuple with (photo_id, photo_url)
        """
        flickr_url = util.posse_post_discovery(original, FLICKR_PHOTO_RE)
        if flickr_url:
            m = FLICKR_PHOTO_RE.match(flickr_url)
            if m:
                return m.group(2), flickr_url
        return None, None

    def get_path_alias():
        return (site.account.user_info.get('person', {}).get('path_alias') or
                site.account.user_id)

    in_reply_to = request.form.get('in-reply-to')
    like_of = request.form.get('like-of')

    title = request.form.get('name')
    desc = request.form.get('content[value]') or request.form.get('content')

    # try to comment on a photo
    if in_reply_to:
        photo_id, flickr_url = get_photo_id(in_reply_to)
        if not photo_id:
            return util.make_publish_error_response(
                'Could not find Flickr photo to comment on based on URL {}'
                .format(in_reply_to))
        r = call_api_method('POST', 'flickr.photos.comments.addComment', {
            'photo_id': photo_id,
            'comment_text': desc or title,
        }, site=site)
        result = r.json()
        if result.get('stat') == 'fail':
            return util.wrap_silo_error_response(r)
        return util.make_publish_success_response(
            result.get('comment', {}).get('permalink'), result)

    # try to like a photo
    if like_of:
        photo_id, flickr_url = get_photo_id(like_of)
        if not photo_id:
            return util.make_publish_error_response(
                'Could not find Flickr photo to like based on original URL {}'
                .format(like_of))
        r = call_api_method('POST', 'flickr.favorites.add', {
            'photo_id': photo_id,
        }, site=site)
        result = r.json()
        if result.get('stat') == 'fail':
            return util.wrap_silo_error_response(r)
        return util.make_publish_success_response(
            flickr_url + '#liked-by-' + get_path_alias(), result)

    # otherwise we're uploading a photo
    photo_file = util.get_first(util.get_files_or_urls_as_file_storage(request.files, request.form, 'photo'))
    if not photo_file:
        photo_file = util.get_first(util.get_files_or_urls_as_file_storage(request.files, request.form, 'video'))

    if not photo_file:
        return util.make_publish_error_response('Missing "photo" attachment')

    r = upload({'title': title, 'description': desc}, photo_file,
               site=site)

    if r.status_code // 100 != 2:
        return util.wrap_silo_error_response(r)

    photo_id, error = interpret_upload_response(r)
    if error:
        return util.make_publish_error_response(error)

    # maybe add some tags or people
    cats = util.get_possible_array_value(request.form, 'category')
    tags = []
    user_ids = []

    for cat in cats:
        if util.looks_like_a_url(cat):
            resp = call_api_method(
                'GET', 'flickr.urls.lookupUser', {'url': cat}, site=site)
            if resp.status_code // 100 != 2:
                current_app.logger.error(
                    'Error looking up user by url %s. Response: %r, %s',
                    cat, resp, resp.text)
            result = resp.json()
            if result.get('stat') == 'fail':
                current_app.logger.debug(
                    'User not found for url %s', cat)
            else:
                user_id = result.get('user', {}).get('id')
                if user_id:
                    user_ids.append(user_id)
        else:
            tags.append('"' + cat + '"')

    if tags:
        current_app.logger.debug('Adding tags: %s', ','.join(tags))
        resp = call_api_method('POST', 'flickr.photos.addTags', {
            'photo_id': photo_id,
            'tags': ','.join(tags),
        }, site=site)
        current_app.logger.debug('Added tags: %r, %s', resp, resp.text)

    for user_id in user_ids:
        current_app.logger.debug('Tagging user id: %s', user_id)
        resp = call_api_method('POST', 'flickr.photos.people.add', {
            'photo_id': photo_id,
            'user_id': user_id,
        }, site=site)
        current_app.logger.debug('Tagged person: %r, %s', resp, resp.text)

    lat, lng = util.parse_geo_uri(request.form.get('location'))
    if lat and lng:
        current_app.logger.debug('setting location: %s, %s', lat, lng)
        resp = call_api_method('POST', 'flickr.photos.geo.setLocation', {
            'photo_id': photo_id,
            'lat': lat,
            'lon': lng,
        }, site=site)
        current_app.logger.debug('set location: %r, %s', resp, resp.text)

    return util.make_publish_success_response(
        'https://www.flickr.com/photos/{}/{}/'.format(
            get_path_alias(), photo_id))
