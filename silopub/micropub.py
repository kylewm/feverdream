from silopub import util
from silopub.ext import csrf, redis
from silopub.models import Site, Token
from flask import Blueprint, redirect, url_for, current_app, request, abort
from flask import session, make_response
import datetime
import json
import sys
import binascii
import os
import urllib

SERVICES = {}
micropub = Blueprint('micropub', __name__)


def register_service(name, service):
    """Register the services by name so we can delegate to their specific
    implementations
    """
    SERVICES[name] = service


def guess_service(me):
    me = deproxyify(me)
    service = None

    # if we've previously registered, then it's easy
    site = Site.lookup_by_url(me)
    if site:
        service = site.service
    else:
        # otherwise we have to take an educated guess
        domain = util.domain_for_url(me).lower()
        current_app.logger.debug('guessing service by domain %s', domain)
        if domain.endswith('tumblr.com'):
            service = 'tumblr'
        elif domain.endswith('wordpress.com'):
            service = 'wordpress'
        elif domain.endswith('blogger.com'):
            service = 'blogger'
        elif domain == 'twitter.com':
            service = 'twitter'
        elif domain == 'facebook.com':
            service = 'facebook'
        elif domain == 'flickr.com':
            service = 'flickr'
        elif domain == 'github.com':
            service = 'github'
        elif domain == 'goodreads.com':
            service = 'goodreads'

    return service and SERVICES[service]


@csrf.exempt
@micropub.route('/indieauth', methods=['GET', 'POST'])
def indieauth():
    # verify authorization
    if request.method == 'POST':
        code = request.form.get('code')
        client_id = request.form.get('client_id')
        redirect_uri = request.form.get('redirect_uri')

        datastr = redis.get('indieauth-code:{}'.format(code))
        if not datastr:
            current_app.logger.warn('unrecognized auth code %s', code)
            return util.urlenc_response(
                {'error': 'Unrecognized or expired authorization code'}, 400)

        data = json.loads(datastr.decode('utf-8'))
        for key, value in [('client_id', client_id),
                           ('redirect_uri', redirect_uri)]:
            if data.get(key) != value:
                current_app.logger.warn(
                    '%s mismatch. expected=%s, received=%s',
                    key, data.get(key), value)
                return util.urlenc_response({'error': key + ' mismatch'}, 400)

        me = data.get('me')
        return util.urlenc_response({'me': me})

    # indieauth via the silo's authenication mechanism
    try:
        me = request.args.get('me')
        redirect_uri = request.args.get('redirect_uri')

        current_app.logger.info('get indieauth with me=%s and redirect=%s', me,
                                redirect_uri)
        if not me or not redirect_uri:
            resp = make_response(
                "This is SiloPub's authorization endpoint. At least 'me' and "
                "'redirect_uri' are required.")
            resp.headers['IndieAuth'] = 'authorization_endpoint'
            return resp

        session['indieauth_params'] = {
            'me': me,
            'redirect_uri': redirect_uri,
            'client_id': request.args.get('client_id'),
            'state': request.args.get('state', ''),
            'scope': request.args.get('scope', ''),
        }

        service = guess_service(me)
        if not service:
            resp = make_response("Could not find a service for URL {}"
                                 .format(me))
            return resp

        return redirect(service.get_authorize_url(
            url_for('.indieauth_callback', _external=True), me=me))

    except:
        current_app.logger.exception('Starting IndieAuth')
        if not redirect_uri:
            resp = make_response('Exception starting indieauth: {}'.format(
                str(sys.exc_info()[0])), 400)
            resp.headers['Content-Type'] = 'text/plain'
            return resp

        return redirect(util.set_query_params(
            redirect_uri, error=str(sys.exc_info()[0])))


@micropub.route('/indieauth/callback')
def indieauth_callback():
    ia_params = session.get('indieauth_params', {})
    me = ia_params.get('me')
    client_id = ia_params.get('client_id')
    redirect_uri = ia_params.get('redirect_uri')
    state = ia_params.get('state', '')
    scope = ia_params.get('scope', '')

    if not me:
        current_app.logger.warn('No "me" value in session')
        return abort(400)

    service = guess_service(me)
    if not service:
        current_app.logger.warn('Could not guess service for URL %s', me)
        return abort(400)

    result = service.process_callback(
        url_for('.indieauth_callback', _external=True))

    if 'error' in result:
        current_app.logger.warn('error on callback %s', result['error'])
        return redirect(
            util.set_query_params(redirect_uri, error=result['error']))

    parsed = urllib.parse.urlparse(deproxyify(me))
    domain = parsed.netloc
    if parsed.path != '/':
        domain += parsed.path

    account = result['account']
    my_site = result.get('site') or next(
        (s for s in account.sites if s.domain == domain), None)

    if not my_site:
        error = 'Authed account does not have a site for domain {}'.format(
            domain)
        current_app.logger.warn('error on callback %s', error)
        return redirect(
            util.set_query_params(redirect_uri, error=error))

    current_app.logger.debug('auth callback got account %s and site %s',
                             account, my_site)

    # hand back a code to the micropub client
    code = binascii.hexlify(os.urandom(16)).decode()
    redis.setex('indieauth-code:{}'.format(code),
                datetime.timedelta(minutes=5),
                json.dumps({
                    'site': my_site.id,
                    'me': me,
                    'redirect_uri': redirect_uri,
                    'client_id': client_id,
                    'state': state,
                    'scope': scope,
                }))

    return redirect(util.set_query_params(
        redirect_uri, me=me, state=state, code=code))


@csrf.exempt
@micropub.route('/token', methods=['POST'])
def token_endpoint():
    code = request.form.get('code')
    me = request.form.get('me')
    redirect_uri = request.form.get('redirect_uri')
    client_id = request.form.get('client_id')

    datastr = redis.get('indieauth-code:{}'.format(code))
    if not datastr:
        current_app.logger.warn('unrecognized or expired code %s', code)
        return util.urlenc_response(
            {'error': 'Unrecognized or expired authorization code'}, 400)

    data = json.loads(datastr.decode('utf-8'))
    for key, value in [('me', me), ('client_id', client_id),
                       ('redirect_uri', redirect_uri)]:
        if data.get(key) != value:
            current_app.logger.warn('%s mismatch. expected=%s, received=%s',
                                    key, data.get(key), value)
            return util.urlenc_response({'error': key + ' mismatch'}, 400)

    # ok we're confirmed, create an access token
    scope = data.get('scope', '')
    site_id = data.get('site')
    site = Site.query.get(site_id)

    if not site_id or not site:
        return util.urlenc_response(
            {'error': 'No site for authorization code!'}, 400)

    token = Token.create_or_update(site, scope, client_id)
    return util.urlenc_response({
        'access_token': token.token,
        'me': me,
        'scope': scope,
    })


@csrf.exempt
@micropub.route('/micropub', methods=['GET', 'POST'])
def micropub_endpoint():
    current_app.logger.info(
        "received micropub request %s, args=%s, form=%s, headers=%s",
        request, request.args, request.form, request.headers)

    if request.method == 'GET':
        return 'This is the micropub endpoint.'

    bearer_prefix = 'Bearer '
    header_token = request.headers.get('authorization')
    if header_token and header_token.startswith(bearer_prefix):
        token = header_token[len(bearer_prefix):]
    else:
        token = request.form.get('access_token')

    if not token:
        err_msg = 'Micropub request is missing access_token'
        current_app.logger.warn(err_msg)
        return err_msg, 401

    # indieauth has confirmed that me is who they say they are, so if we have
    # an access token for their api, then we should be good to go

    # new short tokens
    token_data = Token.query.get(token)
    if token_data:
        site = token_data.site

    # old JWT tokens (TODO how do I get rid of these??)
    else:
        try:
            token_data = util.jwt_decode(token)
        except:
            current_app.logger.debug('could not find or decode token: %s', token)

        if not token_data:
            err_msg = 'Unrecognized token: {}' .format(token)
            current_app.logger.warn(err_msg)
            return err_msg, 401
        site_id = token_data.get('site')
        site = Site.query.get(site_id)

        if not site:
            err_msg = 'Could not find a site for site id {}.'.format(site_id)
            current_app.logger.warn(err_msg)
            return err_msg, 401

    current_app.logger.info('Success! Publishing to %s', site)
    resp = SERVICES[site.service].publish(site)
    if not resp:
        return util.make_publish_error_response(
            'Unspecified failure. Maybe an invalid or unsupported request?')
    return resp


def deproxyify(url):
    if url.startswith(request.url_root):
        return 'http://' + url[len(request.url_root):]
    return url
