from flask import Blueprint, flash, redirect, url_for, request, current_app
from flask_wtf.csrf import generate_csrf, validate_csrf
import requests
import html
import sys
from urllib.parse import urlencode, parse_qs
from silopub.models import Account, GitHub
from silopub.ext import db
from silopub import util
import re

SERVICE_NAME = 'github'
PERMISSION_SCOPES = 'repo'

BASE_PATTERN = 'https?://(?:www\.)?github\.com/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)'
REPO_PATTERN = BASE_PATTERN + '/?(?:#|$)'
ISSUES_PATTERN = BASE_PATTERN + '/issues/?(?:#|$)'
ISSUE_PATTERN = BASE_PATTERN + '/issues/(\d+)/?(?:#|$)'
PULL_PATTERN = BASE_PATTERN + '/pull/(\d+)/?(?:#|$)'


github = Blueprint('github', __name__)


@github.route('/github.com/<username>')
def proxy_homepage(username):
    account = Account.query.filter_by(
        service=SERVICE_NAME, username=username).first()

    if not account:
        account = Account.query.filter_by(
            service=SERVICE_NAME, user_id=username).first()

    params = {
        'service_name': 'GitHub',
        'service_url': 'https://github.com/',
        'service_photo': 'https://github.com/apple-touch-icon.png',
    }

    if account:
        params.update({
            'user_name': account.username,
            'user_url': account.sites[0].url,
            'user_photo': (account.user_info or {}).get('avatar_url'),
        })
    else:
        params.update({
            'user_name': username,
            'user_url': 'https://github.com/' + username,
        })

    return util.render_proxy_homepage(**params)


@github.route('/github/authorize', methods=['POST'])
def authorize():
    try:
        callback_uri = url_for('.callback', _external=True)
        return redirect(get_authorize_url(callback_uri))
    except:
        current_app.logger.exception('Starting GitHub authorization')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


@github.route('/github/callback')
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
        current_app.logger.exception('Handling GitHub authorization callback')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


def get_authorize_url(callback_uri, **kwargs):
    return 'https://github.com/login/oauth/authorize?' + urlencode({
        'client_id': current_app.config['GITHUB_CLIENT_ID'],
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
        return {'error': 'GitHub auth canceled or failed with error: {}, '
                'description: {}'.format(error, error_desc)}

    if not validate_csrf(state):
        return {'error': 'csrf token mismatch in GitHub callback.'}

    r = requests.post('https://github.com/login/oauth/access_token', data={
        'client_id': current_app.config['GITHUB_CLIENT_ID'],
        'client_secret': current_app.config['GITHUB_CLIENT_SECRET'],
        'code': code,
        'redirect_uri': callback_uri,
        'state': state,
    })

    payload = parse_qs(r.text)
    current_app.logger.debug('auth responses from GitHub %s', payload)
    access_token = payload['access_token'][0]

    r = requests.get('https://api.github.com/user', headers={
        'Authorization': 'token ' + access_token,
    })

    user_info = r.json()
    user_id = str(user_info.get('id'))

    account = Account.query.filter_by(
        service='github', user_id=user_id).first()

    if not account:
        account = Account(service='github', user_id=user_id)
        db.session.add(account)

    account.username = user_info.get('login')
    account.token = access_token
    account.user_info = user_info

    account.update_sites([GitHub(
        url='https://github.com/{}'.format(account.username),
        # overloading "domain" to really mean "user's canonical url"
        domain='github.com/{}'.format(account.username),
        site_id=account.user_id)])

    db.session.commit()
    util.set_authed(account.sites)

    return {'account': account}


def publish(site):
    auth_headers = {'Authorization': 'token ' + site.account.token}

    in_reply_to = request.form.get('in-reply-to')
    if in_reply_to:
        in_reply_to = util.posse_post_discovery(in_reply_to, BASE_PATTERN)

        repo_match = (re.match(REPO_PATTERN, in_reply_to) or
                      re.match(ISSUES_PATTERN, in_reply_to))
        issue_match = (re.match(ISSUE_PATTERN, in_reply_to) or
                       re.match(PULL_PATTERN, in_reply_to))

        # reply to a repository -- post a new issue
        if repo_match:
            endpoint = 'https://api.github.com/repos/{}/{}/issues'.format(
                repo_match.group(1), repo_match.group(2))

            title = request.form.get('name')
            body = (request.form.get('content[markdown]') or
                    request.form.get('content[value]') or
                    request.form.get('content') or '')

            if not title and body:
                title = body[:49] + '\u2026'

            data = {
                'title': title,
                'body': body,
                'labels': util.get_possible_array_value(request.form, 'category'),
            }
        # reply to an issue -- post a new comment
        elif issue_match:
            endpoint = 'https://api.github.com/repos/{}/{}/issues/{}/comments'.format(
                issue_match.group(1), issue_match.group(2), issue_match.group(3))
            body = (request.form.get('content[markdown]') or
                    request.form.get('content[value]') or
                    request.form.get('content') or '')
            data = {'body': body}
        else:
            return util.make_publish_error_response(
                'Reply URL does look like a repo or issue: ' + in_reply_to)

        current_app.logger.debug('sending POST to %s with data %s', endpoint, data)
        r = requests.post(endpoint, json=data, headers=auth_headers)

        if r.status_code // 100 != 2:
            return util.wrap_silo_error_response(r)

        resp_json = r.json()
        return util.make_publish_success_response(
            resp_json.get('html_url'), resp_json)

    # like a repository -- star the repository
    like_of = request.form.get('like-of')
    if like_of:
        like_of = util.posse_post_discovery(like_of, BASE_PATTERN)
        repo_match = re.match(REPO_PATTERN, like_of)
        if repo_match:
            endpoint = 'https://api.github.com/user/starred/{}/{}'.format(
                repo_match.group(1), repo_match.group(2))
            current_app.logger.debug('sending PUT to %s', endpoint)
            r = requests.put(endpoint, headers=auth_headers)

            if r.status_code // 100 != 2:
                return util.wrap_silo_error_response(r)

            return util.make_publish_success_response(
                like_of + '#starred-by-' + site.account.username)

        else:
            return util.make_publish_error_response(
                'Like-of URL must be a repo: ' + like_of)

        return util.make_publish_error_response(
            'See {} for details publishing to GitHub.'
            .format(url_for('views.developers', _external=True)))
