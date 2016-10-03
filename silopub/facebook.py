from flask import Blueprint, current_app, redirect, url_for, request, flash
from flask_wtf.csrf import generate_csrf, validate_csrf
from silopub import util
from silopub.ext import db
from silopub.models import Account, Facebook
from urllib.parse import urlencode, parse_qs
import brevity
import html
import requests
import sys

SERVICE_NAME = 'facebook'
PERMISSION_SCOPES = 'publish_actions,user_videos'


facebook = Blueprint('facebook', __name__)


@facebook.route('/facebook.com/<user_id>')
def proxy_homepage(user_id):
    account = Account.query.filter_by(
        service=SERVICE_NAME, user_id=user_id).first()

    params = {
        'service_name': 'Facebook',
        'service_url': 'https://www.facebook.com/',
        'service_photo': 'https://static.xx.fbcdn.net/rsrc.php/yl/r/H3nktOa7ZMg.ico',
    }

    if account:
        info = account.user_info or {}
        params.update({
            'user_name': info.get('name') or account.username,
            'user_url': account.sites[0].url,
            'user_photo': info.get('picture', {}).get('data', {}).get('url'),
        })
    else:
        params['user_name'] = user_id

    return util.render_proxy_homepage(**params)


@facebook.route('/facebook/authorize', methods=['POST'])
def authorize():
    try:
        current_app.logger.debug('Redirecting to Facebook authorize')
        callback_uri = url_for('.callback', _external=True)
        return redirect(get_authorize_url(callback_uri))
    except:
        current_app.logger.exception('Starting Facebook authorization')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


@facebook.route('/facebook/callback')
def callback():
    callback_uri = url_for('.callback', _external=True)
    result = process_callback(callback_uri)

    if 'error' in result:
        flash(result['error'], category='danger')
        return redirect(url_for('views.index'))

    account = result['account']
    return redirect(url_for('views.setup_account', service=SERVICE_NAME,
                            user_id=account.user_id))


def get_authorize_url(callback_uri, **kwargs):
    return 'https://graph.facebook.com/oauth/authorize?' + urlencode({
        'client_id': current_app.config['FACEBOOK_CLIENT_ID'],
        'redirect_uri': callback_uri,
        'scope': PERMISSION_SCOPES,
        'state': generate_csrf(),
    })


def process_callback(callback_uri):
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_desc = request.args.get('error_description', '')

    if error:
        return {'error': 'Facebook auth canceled or failed with error: {}, '
                'description: {}'.format(error, error_desc)}

    if not validate_csrf(state):
        return {'error': 'csrf token mismatch in Facebook callback.'}

    r = requests.get('https://graph.facebook.com/oauth/access_token', params={
        'client_id': current_app.config['FACEBOOK_CLIENT_ID'],
        'client_secret': current_app.config['FACEBOOK_CLIENT_SECRET'],
        'redirect_uri': callback_uri,
        'code': code,
        'scope': PERMISSION_SCOPES,
    })

    if r.status_code // 100 != 2:
        error_obj = r.json()
        error = error_obj.get('error')
        error_desc = error_obj.get('error_description')
        return {'error': 'Error ({}) requesting access token: {}, '
                'description: {}' .format(r.status_code, error, error_desc)}

    payload = parse_qs(r.text)
    current_app.logger.debug('auth responses from Facebook %s', payload)
    access_token = payload['access_token'][0]

    r = requests.get('https://graph.facebook.com/v2.5/me', params={
        'access_token': access_token,
        'fields': 'id,name,picture',
    })

    if r.status_code // 100 != 2:
        error_obj = r.json()
        error = error_obj.get('error')
        error_desc = error_obj.get('error_description')
        return {'error': 'Error ({}) requesting authed user info: {}, '
                'description: {}' .format(r.status_code, error, error_desc)}

    user_info = r.json()
    current_app.logger.debug('authed user info from Facebook %s', user_info)

    user_id = user_info.get('id')
    account = Account.query.filter_by(
        service='facebook', user_id=user_id).first()

    if not account:
        account = Account(service='facebook', user_id=user_id,
                          username=user_id)
        db.session.add(account)

    account.user_info = user_info
    account.token = access_token

    account.update_sites([Facebook(
        url='https://www.facebook.com/{}'.format(account.user_id),
        # overloading "domain" to really mean "user's canonical url"
        domain='facebook.com/{}'.format(account.user_id),
        site_id=account.user_id)])

    db.session.commit()
    util.set_authed(account.sites)
    return {'account': account}


@facebook.route('/facebook/username', methods=['POST'])
def custom_username():
    next_url = request.form.get('next')
    site_id = request.form.get('site')
    site = Facebook.query.get(site_id)
    if not site or not util.is_authed(site):
        flash('Please authenticate with this service', 'warning')
    else:
        site.account.username = request.form.get('username')
        db.session.commit()
        flash('Updated username to ' + site.account.username)
    return redirect(next_url)


def publish(site):
    title = request.form.get('name')
    content = request.form.get('content[value]') or request.form.get('content')
    permalink = request.form.get('url')

    photo_file = util.get_first(util.get_possible_array_value(request.files, 'photo'))
    photo_url = util.get_first(util.get_possible_array_value(request.form, 'photo'))

    video_file = util.get_first(util.get_possible_array_value(request.files, 'video'))
    video_url = util.get_first(util.get_possible_array_value(request.form, 'video'))

    location = request.form.get('location')

    post_data = {'access_token': site.account.token}
    post_files = None
    api_endpoint = 'https://graph.facebook.com/v2.5/me/feed'
    fburl_separator = 'posts'

    message = (
        content if not permalink else
        '({})'.format(permalink) if not content else
        '{} ({})'.format(content, permalink))

    if video_file or video_url:
        if video_file:
            post_files = {'source': (video_file.filename, video_file.stream,
                                     video_file.content_type or 'video/mp4')}
        elif video_url:
            post_data['url'] = video_url
        post_data['title'] = title
        post_data['description'] = message
        api_endpoint = 'https://graph-video.facebook.com/v2.5/me/videos'
        fburl_separator = 'videos'
    elif photo_file or photo_url:
        if photo_file:
            post_files = {'source': photo_file}
        elif photo_url:
            post_data['url'] = photo_url
        post_data['caption'] = message
        # TODO support album id as alternative to 'me'
        # TODO upload to "Timeline photos" album by default
        api_endpoint = 'https://graph.facebook.com/v2.5/me/photos'
        fburl_separator = 'photos'
    elif title and content:
        # looks like an article -- include the permalink as a 'link'
        # instead of inline
        post_data['message'] = '{}\n\n{}'.format(title, content)
        post_data['link'] = permalink
        post_data['name'] = title
    elif content:
        post_data['message'] = message
        tokens = brevity.tokenize(content)
        # linkify the first url in the message
        linktok = next((tok for tok in tokens if tok.tag == 'link'), None)
        if linktok:
            post_data['link'] = linktok.content
    else:
        return util.make_publish_error_response(
            'Request must contain a photo, video, or content')

    # posting Location to Facebook is disabled for now -- just
    # searching lat/long does not get us close enough to assume we
    # have the correct place.
    if False and location:
        if location.isnumeric():
            post_data['place'] = location
        else:
            place_name = (request.form.get('place-name') or
                          request.form.get('place_name'))
            lat, lng = util.parse_geo_uri(location)
            if lat and lng:
                current_app.logger.debug('Search FB for a place, %s at %s, %s',
                                         place_name, lat, lng)
                r = requests.get(
                    'https://graph.facebook.com/v2.5/search',
                    params=util.trim_nulls({
                        'type': 'place',
                        'center': '%s,%s' % (lat, lng),
                        'distance': '500',
                        'q': place_name,
                        'access_token': site.account.token,
                    }))
                if r.status_code != 200:
                    current_app.logger.warning(
                        'FB place search failed with response %r: %r',
                        r, r.text)
                else:
                    places = r.json().get('data', [])
                    if not places:
                        # TODO consider searching without a place name?
                        current_app.logger.warning(
                            'FB no resuts for place %s at %s, %s ',
                            place_name, lat, lng)
                    else:
                        current_app.logger.debug(
                            'Found FB place: %s (%s)', places[0].get('name'),
                            places[0].get('id'))
                        post_data['place'] = places[0].get('id')

    post_data = util.trim_nulls(post_data)
    current_app.logger.debug(
        'Publishing to facebook %s: data=%s, files=%s', api_endpoint,
        post_data, post_files)
    r = requests.post(api_endpoint, data=post_data, files=post_files)

    # need Web Canvas permissions to do this, which I am too lazy to apply for
    # if r.status_code == 400:
    #     error_data = r.json().get('error', {})
    #     code = error_data.get('code')
    #     subcode = error_data.get('subcode')
    #     # token is expired or otherwise invalid
    #     if code == 190:
    #         send_token_expired_notification(
    #             site.account.user_id,
    #             "silo.pub's Facebook access token has expired. Click the "
    #             "Facebook button on silo.pub's homepage to renew.",
    #             'https://silo.pub/')

    if r.status_code // 100 != 2:
        return util.wrap_silo_error_response(r)

    resp_data = r.json()
    userid = ''
    fbid = resp_data.get('id') or resp_data.get('post_id')

    split = fbid.split('_')
    if len(split) == 2:
        userid, fbid = split

    return util.make_publish_success_response(
        'https://www.facebook.com/{}/{}/{}'.format(
            site.account.username or userid, fburl_separator, fbid),
        data=resp_data)


def send_token_expired_notification(user_id, text, link):
    current_app.logger.debug(
        'Sending Facebook notification: %r, %s', text, link)

    r = requests.post(
        'https://graph.facebook.com/v2.5/{}/notifications'.format(user_id),
        data={
            'template': text,
            'href': link,
            # this is a synthetic app access token.
            # https://developers.facebook.com/docs/facebook-login/access-tokens/#apptokens
            'access_token': '|'.join(
                (current_app.config['FACEBOOK_CLIENT_ID'],
                 current_app.config['FACEBOOK_CLIENT_SECRET']))
        })
    current_app.logger.debug('Response: %s %s', r.status_code, r.text)
