import html
import os
import re
import requests
import sys
import tempfile
import urllib.parse

from flask import Blueprint, current_app, redirect, url_for, request, flash
from flask import session, abort
from requests_oauthlib import OAuth1Session, OAuth1
from silopub import util
from silopub.ext import db
from silopub.models import Account, Twitter
import brevity

REQUEST_TOKEN_URL = 'https://api.twitter.com/oauth/request_token'
AUTHENTICATE_URL = 'https://api.twitter.com/oauth/authenticate'
AUTHORIZE_URL = 'https://api.twitter.com/oauth/authorize'
ACCESS_TOKEN_URL = 'https://api.twitter.com/oauth/access_token'
VERIFY_CREDENTIALS_URL = 'https://api.twitter.com/1.1/account/verify_credentials.json'
CREATE_STATUS_URL = 'https://api.twitter.com/1.1/statuses/update.json'

#CREATE_WITH_MEDIA_URL = 'https://api.twitter.com/1.1/statuses/update_with_media.json'
UPLOAD_MEDIA_URL = 'https://upload.twitter.com/1.1/media/upload.json'
RETWEET_STATUS_URL = 'https://api.twitter.com/1.1/statuses/retweet/{}.json'
FAVE_STATUS_URL = 'https://api.twitter.com/1.1/favorites/create.json'


TWEET_RE = re.compile(r'https?://(?:www\.|mobile\.)?twitter\.com/(\w+)/status(?:es)?/(\w+)')

SERVICE_NAME = 'twitter'

twitter = Blueprint('twitter', __name__)


@twitter.route('/twitter.com/<username>')
def proxy_homepage(username):
    account = Account.query.filter_by(
        service=SERVICE_NAME, username=username).first()

    params = {
        'service_name': 'Twitter',
        'service_url': 'https://twitter.com/',
        'service_photo': 'https://abs.twimg.com/favicons/favicon.ico',
    }

    if account:
        params.update({
            'user_name': '@' + account.username,
            'user_url': account.sites[0].url,
            'user_photo': (account.user_info or {}).get(
                'profile_image_url_https'),
        })
    else:
        params.update({
            'user_name': '@' + username,
            'user_url': 'https://twitter.com/' + username,
        })

    return util.render_proxy_homepage(**params)


@twitter.route('/twitter/authorize', methods=['POST'])
def authorize():
    try:
        callback_uri = url_for('.callback', _external=True)
        return redirect(get_authorize_url(callback_uri))
    except:
        current_app.logger.exception('Starting Twitter authorization')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


@twitter.route('/twitter/callback')
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
        current_app.logger.exception('During Twitter authorization callback')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


def get_authorize_url(callback_uri, me=None, **kwargs):
    session.pop('oauth_token', None)
    session.pop('oauth_token_secret', None)
    oauth_session = OAuth1Session(
        client_key=current_app.config['TWITTER_CLIENT_KEY'],
        client_secret=current_app.config['TWITTER_CLIENT_SECRET'],
        callback_uri=callback_uri)

    r = oauth_session.fetch_request_token(REQUEST_TOKEN_URL)
    session['oauth_token'] = r.get('oauth_token')
    session['oauth_token_secret'] = r.get('oauth_token_secret')
    params = {'force_login': 'true'}
    if me:
        params['screen_name'] = me.split('/')[-1]
    return oauth_session.authorization_url(
        AUTHORIZE_URL + '?' + urllib.parse.urlencode(params))


def process_callback(callback_uri):
    verifier = request.args.get('oauth_verifier')
    if not verifier:
        # user declined
        return {'error': 'Twitter authorization declined'}

    request_token = session.get('oauth_token')
    request_token_secret = session.get('oauth_token_secret')
    oauth_session = OAuth1Session(
        client_key=current_app.config['TWITTER_CLIENT_KEY'],
        client_secret=current_app.config['TWITTER_CLIENT_SECRET'],
        resource_owner_key=request_token,
        resource_owner_secret=request_token_secret,
        callback_uri=callback_uri)
    oauth_session.parse_authorization_response(request.url)
    # get the access token and secret
    r = oauth_session.fetch_access_token(ACCESS_TOKEN_URL)
    access_token = r.get('oauth_token')
    access_token_secret = r.get('oauth_token_secret')

    current_app.logger.debug('request token: %s, secret: %s',
                             request_token, request_token_secret)
    current_app.logger.debug('access token: %s, secret: %s',
                             access_token, access_token_secret)

    auth = OAuth1(
        client_key=current_app.config['TWITTER_CLIENT_KEY'],
        client_secret=current_app.config['TWITTER_CLIENT_SECRET'],
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret)

    user_info = requests.get(VERIFY_CREDENTIALS_URL, auth=auth).json()

    if 'errors' in user_info:
        return {'error': 'Error fetching credentials %r'
                % user_info.get('errors')}

    user_id = user_info.get('id_str')
    username = user_info.get('screen_name')

    current_app.logger.debug('verified credentials. user_id=%s, username=%s',
                             user_id, username)
    current_app.logger.debug('user_info: %r', user_info)

    account = Account.query.filter_by(
        service='twitter', user_id=user_id).first()

    if not account:
        account = Account(service='twitter', user_id=user_id)
        db.session.add(account)

    account.username = username
    account.user_info = user_info
    account.token = access_token
    account.token_secret = access_token_secret

    account.update_sites([Twitter(
        url='https://twitter.com/{}'.format(account.username),
        domain='twitter.com/{}'.format(account.username),
        site_id=account.user_id)])

    db.session.commit()
    util.set_authed(account.sites)
    return {'account': account}


def publish(site):
    auth = OAuth1(
        client_key=current_app.config['TWITTER_CLIENT_KEY'],
        client_secret=current_app.config['TWITTER_CLIENT_SECRET'],
        resource_owner_key=site.account.token,
        resource_owner_secret=site.account.token_secret)

    def interpret_response(result):
        if result.status_code // 100 != 2:
            return util.wrap_silo_error_response(result)

        result_json = result.json()
        twitter_url = 'https://twitter.com/{}/status/{}'.format(
            result_json.get('user', {}).get('screen_name'),
            result_json.get('id_str'))
        return util.make_publish_success_response(twitter_url, result_json)

    def get_tweet_id(original):
        tweet_url = util.posse_post_discovery(original, TWEET_RE)
        if tweet_url:
            m = TWEET_RE.match(tweet_url)
            if m:
                return m.group(1), m.group(2)
        return None, None

    def upload_photo(photo):
        current_app.logger.debug('uploading photo, name=%s, type=%s',
                                 photo.filename, photo.content_type)
        result = requests.post(UPLOAD_MEDIA_URL, files={
            'media': (photo.filename, photo.stream, photo.content_type),
        }, auth=auth)
        if result.status_code // 100 != 2:
            return None, result
        result_data = result.json()
        current_app.logger.debug('upload result: %s', result_data)
        return result_data.get('media_id_string'), None

    def upload_video(video, default_content_type='video/mp4'):
        # chunked video upload
        chunk_files = []

        def cleanup():
            for f in chunk_files:
                os.unlink(f)

        chunk_size = 1 << 20
        total_size = 0
        while True:
            chunk = video.read(chunk_size)
            if not chunk:
                break
            total_size += len(chunk)

            tempfd, tempfn = tempfile.mkstemp('-%03d-%s' % (
                len(chunk_files), video.filename))
            with open(tempfn, 'wb') as f:
                f.write(chunk)
            chunk_files.append(tempfn)

        current_app.logger.debug('init upload. type=%s, length=%s',
                                 video.content_type, video.content_length)
        result = requests.post(UPLOAD_MEDIA_URL, data={
            'command': 'INIT',
            'media_type': video.content_type or default_content_type,
            'total_bytes': total_size,
        }, auth=auth)
        current_app.logger.debug('init result: %s %s', result, result.text)
        if result.status_code // 100 != 2:
            cleanup()
            return None, result
        result_data = result.json()
        media_id = result_data.get('media_id_string')
        segment_idx = 0

        for chunk_file in chunk_files:
            current_app.logger.debug('appending file: %s', chunk_file)
            result = requests.post(UPLOAD_MEDIA_URL, data={
                'command': 'APPEND',
                'media_id': media_id,
                'segment_index': segment_idx,
            }, files={
                'media': open(chunk_file, 'rb'),
            }, auth=auth)
            current_app.logger.debug(
                'append result: %s %s', result, result.text)
            if result.status_code // 100 != 2:
                cleanup()
                return None, result
            segment_idx += 1

        current_app.logger.debug('finalize uploading video: %s', media_id)
        result = requests.post(UPLOAD_MEDIA_URL, data={
            'command': 'FINALIZE',
            'media_id': media_id,
        }, auth=auth)
        current_app.logger.debug('finalize result: %s %s', result, result.text)
        if result.status_code // 100 != 2:
            cleanup()
            return None, result
        cleanup()
        return media_id, None

    data = {}
    format = brevity.FORMAT_NOTE
    content = request.form.get('content[value]') or request.form.get('content')

    if 'name' in request.form:
        format = brevity.FORMAT_ARTICLE
        content = request.form.get('name')

    repost_ofs = util.get_possible_array_value(request.form, 'repost-of')
    for repost_of in repost_ofs:
        _, tweet_id = get_tweet_id(repost_of)
        if tweet_id:
            return interpret_response(
                requests.post(RETWEET_STATUS_URL.format(tweet_id), auth=auth))
    else:
        if repost_ofs:
            content = 'Reposted: {}'.format(repost_ofs[0])

    like_ofs = util.get_possible_array_value(request.form, 'like-of')
    for like_of in like_ofs:
        _, tweet_id = get_tweet_id(like_of)
        if tweet_id:
            return interpret_response(
                requests.post(FAVE_STATUS_URL, data={'id': tweet_id}, auth=auth))
    else:
        if like_ofs:
            content = 'Liked: {}'.format(like_ofs[0])

    media_ids = []
    for photo in util.get_files_or_urls_as_file_storage(request.files, request.form, 'photo'):
        media_id, err = upload_photo(photo)
        if err:
            return util.wrap_silo_error_response(err)
        media_ids.append(media_id)

    for video in util.get_files_or_urls_as_file_storage(request.files, request.form, 'video'):
        media_id, err = upload_video(video)
        if err:
            return util.wrap_silo_error_response(err)
        media_ids.append(media_id)

    in_reply_tos = util.get_possible_array_value(request.form, 'in-reply-to')
    for in_reply_to in in_reply_tos:
        twitterer, tweet_id = get_tweet_id(in_reply_to)
        if tweet_id:
            data['in_reply_to_status_id'] = tweet_id
            if (twitterer != site.account.username
                    and '@' + twitterer.lower() not in content.lower()):
                content = '@{} {}'.format(twitterer, content)
            break
    else:
        if in_reply_tos:
            content = 'Re: {}, {}'.format(in_reply_tos[0], content)

    location = request.form.get('location')
    current_app.logger.debug('received location param: %s', location)
    data['lat'], data['long'] = util.parse_geo_uri(location)

    permalink_url = request.form.get('url')
    if media_ids:
        data['media_ids'] = ','.join(media_ids)

    if content:
        data['status'] = brevity.shorten(content, permalink=permalink_url,
                                         format=format)
    data = util.trim_nulls(data)
    current_app.logger.debug('publishing with params %s', data)
    return interpret_response(
        requests.post(CREATE_STATUS_URL, data=data, auth=auth))
