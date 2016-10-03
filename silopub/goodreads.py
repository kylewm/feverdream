import html
import re
import requests
import sys
import urllib.parse
import xml.etree.ElementTree as ETree

from flask import Blueprint, current_app, redirect, url_for, request, flash
from flask import session
from requests_oauthlib import OAuth1Session, OAuth1
from silopub import util
from silopub.ext import db
from silopub.models import Account, Goodreads


SERVICE_NAME = 'goodreads'

REQUEST_TOKEN_URL = 'https://www.goodreads.com/oauth/request_token'
AUTHORIZE_URL = 'https://www.goodreads.com/oauth/authorize'
ACCESS_TOKEN_URL = 'https://www.goodreads.com/oauth/access_token'

SEARCH_URL = 'https://www.goodreads.com/search/index.xml'
REVIEW_CREATE_URL = 'https://www.goodreads.com/review.xml'
SHELVES_LIST_URL = 'https://www.goodreads.com/shelf/list.xml'
ADD_BOOKS_TO_SHELVES_URL = 'https://www.goodreads.com/shelf/add_books_to_shelves.xml'

BOOK_URL_RE = re.compile('https?://(?:www\.)?goodreads\.com/book/show/(\d+)(?:\.(.*))?')


goodreads = Blueprint('goodreads', __name__)


@goodreads.route('/goodreads.com/<user_id>')
def proxy_homepage(user_id):
    account = Account.query.filter_by(
        service=SERVICE_NAME, user_id=user_id).first()

    params = dict(
        service_name='Goodreads',
        service_url='https://www.goodreads.com/',
        service_photo='https://www.goodreads.com/favicon.ico')

    if account:
        params.update(dict(
            user_name=account.username,
            user_url=account.sites[0].url,
            user_photo=account.user_info and account.user_info.get('image')))
    else:
        params['user_name'] = user_id

    return util.render_proxy_homepage(**params)


@goodreads.route('/goodreads/authorize', methods=['POST'])
def authorize():
    try:
        current_app.logger.debug('Redirecting to goodreads authorize')
        callback_uri = url_for('.callback', _external=True)
        return redirect(get_authorize_url(callback_uri))
    except:
        current_app.logger.exception('Starting goodreads authorization')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


@goodreads.route('/goodreads/callback')
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
        current_app.logger.exception('goodreads authorization callback')
        flash(html.escape(str(sys.exc_info()[0])), 'danger')
        return redirect(url_for('views.index'))


def get_authorize_url(callback_uri, **kwargs):
    session.pop('oauth_token', None)
    session.pop('oauth_token_secret', None)
    oauth_session = OAuth1Session(
        client_key=current_app.config['GOODREADS_CLIENT_KEY'],
        client_secret=current_app.config['GOODREADS_CLIENT_SECRET'],
        callback_uri=callback_uri)

    r = oauth_session.fetch_request_token(REQUEST_TOKEN_URL)
    session['oauth_token'] = r.get('oauth_token')
    session['oauth_token_secret'] = r.get('oauth_token_secret')
    return oauth_session.authorization_url(
        AUTHORIZE_URL + '?' + urllib.parse.urlencode({
            'oauth_callback': callback_uri,
        }))


def process_callback(callback_uri):
    if request.args.get('authorize') != '1':
        return {'error': 'Goodreads user declined'}

    request_token = session.get('oauth_token')
    request_token_secret = session.get('oauth_token_secret')

    if request_token != request.args.get('oauth_token'):
        return {'error': 'oauth_token does not match'}

    oauth_session = OAuth1Session(
        client_key=current_app.config['GOODREADS_CLIENT_KEY'],
        client_secret=current_app.config['GOODREADS_CLIENT_SECRET'],
        resource_owner_key=request_token,
        resource_owner_secret=request_token_secret,
        callback_uri=callback_uri,
        # Goodreads does not use a verifier, put something here so that
        # the library doesn't error
        verifier='unused')
    oauth_session.parse_authorization_response(request.url)
    # get the access token and secret
    r = oauth_session.fetch_access_token(ACCESS_TOKEN_URL)
    access_token = r.get('oauth_token')
    access_token_secret = r.get('oauth_token_secret')

    r = oauth_session.get('https://www.goodreads.com/api/auth_user')

    if r.status_code // 100 != 2:
        return {
            'error': 'unexpected response from auth.user. status={}, body={}'
            .format(r.status_code, r.text)
        }

    # EXAMPLE RESPONSE
    """<?xml version="1.0" encoding="UTF-8"?>
    <GoodreadsResponse>
      <Request>
        <authentication>true</authentication>
          <key><![CDATA[qRuT5Xit4xERHQGzyq9QSw]]></key>
        <method><![CDATA[api_auth_user]]></method>
      </Request>
      <user id="4544167">
      <name>Kyle Mahan</name>
      <link><![CDATA[https://www.goodreads.com/user/show/4544167-kyle?utm_medium=api]]></link>
    </user>
    </GoodreadsResponse>"""

    root = ETree.fromstring(r.content)
    user = root.find('user')
    user_id = user.attrib['id']
    user_name = user.findtext('name')

    account = Account.query.filter_by(
        service=SERVICE_NAME, user_id=user_id).first()
    if not account:
        account = Account(service=SERVICE_NAME, user_id=user_id)
        db.session.add(account)

    account.username = user_name
    account.token = access_token
    account.token_secret = access_token_secret
    account.user_info = fetch_user_info(account.user_id)

    url = 'https://www.goodreads.com/user/show/' + account.user_id

    account.update_sites([Goodreads(
        url=url,
        domain='goodreads.com/' + account.user_id,
        site_id=account.user_id)])

    db.session.commit()
    util.set_authed(account.sites)
    return {'account': account}


def fetch_user_info(user_id):
    r = requests.get('https://www.goodreads.com/user/show/' + user_id + '.xml', params={
        'key': current_app.config['GOODREADS_CLIENT_KEY'],
    })
    if r.status_code // 100 != 2:
        return {
            'error': 'Failed to fetch user info',
            'upstream-status': r.status_code,
            'upstream-data': r.text,
        }
    root = ETree.fromstring(r.content)
    return {
        'url': root.findtext('user/link'),
        'image': root.findtext('user/image_url'),
    }


def publish(site):
    auth = OAuth1(
        client_key=current_app.config['GOODREADS_CLIENT_KEY'],
        client_secret=current_app.config['GOODREADS_CLIENT_SECRET'],
        resource_owner_key=site.account.token,
        resource_owner_secret=site.account.token_secret)

    # publish a review
    # book_id (goodreads internal id)
    # review[review] (text of the review)
    # review[rating] (0-5) ... 0 = not given, 1-5 maps directly to h-review p-rating
    # review[read_at]  dt-reviewed in YYYY-MM-DD
    # shelf -- check p-category for any that match existing goodreads shelves,
    # TODO consider creating shelves for categories?

    # item might be an ISBN, a Goodreads URL, or just a title

    item = request.form.get('item')
    if not item:
        item_name = request.form.get('item[name]')
        item_author = request.form.get('item[author]')
        if item_name and item_author:
            item = item_name + ' ' + item_author

    rating = request.form.get('rating')
    review = next((request.form.get(key) for key in (
        'description[value]', 'description', 'content[value]',
        'content', 'summary', 'name')), None)
    categories = util.get_possible_array_value(request.form, 'category')

    if not item:
        return util.make_publish_error_response(
            'Expected "item": a URL, ISBN, or book title to review')

    m = item and BOOK_URL_RE.match(item)
    if m:
        book_id = m.group(1)
    else:
        # try searching for item
        r = requests.get(SEARCH_URL, params={
            'q': item,
            'key': current_app.config['GOODREADS_CLIENT_KEY'],
        })
        if r.status_code // 100 != 2:
            return util.wrap_silo_error_response(r)
        root = ETree.fromstring(r.content)
        book = root.find('search/results/work/best_book')
        if not book:
            return {
                'error': 'Goodreads found no results for query: ' + item,
                'upstream-data': r.text
            }
        book_id = book.findtext('id')

    # add book to shelves
    all_shelves = set()
    if categories:
        r = requests.get(SHELVES_LIST_URL, params={
            'key': current_app.config['GOODREADS_CLIENT_KEY'],
            'user_id': site.account.user_id,
        })
        if r.status_code // 100 != 2:
            return util.wrap_silo_error_response(r)
        root = ETree.fromstring(r.content)
        for shelf in root.find('shelves'):
            all_shelves.add(shelf.findtext('name'))

    matched_categories = [c for c in categories if c in all_shelves]
    permalink = 'https://www.goodreads.com/book/show/' + book_id
    resp_data = {}

    # publish a review of the book
    if rating or review:
        current_app.logger.debug('creating a review: book=%s, rating=%s, review=%s', book_id, rating, review)
        r = requests.post(REVIEW_CREATE_URL, data=util.trim_nulls({
            'book_id': book_id,
            'review[rating]': rating,
            'review[review]': review,
            # first shelf that matches
            'shelf': matched_categories.pop(0) if matched_categories else None,
        }), auth=auth)
        if r.status_code // 100 != 2:
            return util.wrap_silo_error_response(r)

        # example response
        """<?xml version="1.0" encoding="UTF-8"?>
        <review>
          <id type="integer">1484927007</id>
          <user-id type="integer">4544167</user-id>
          <book-id type="integer">9361589</book-id>
          <rating type="integer">2</rating>
          <read-status>read</read-status>
          <sell-flag type="boolean">false</sell-flag>
          <review></review>
          <recommendation nil="true"/>
          <read-at type="datetime" nil="true"/>
          <updated-at type="datetime">2015-12-29T21:25:34+00:00</updated-at>
          <created-at type="datetime">2015-12-29T21:25:34+00:00</created-at>
          <comments-count type="integer">0</comments-count>
          <weight type="integer">0</weight>
          <ratings-sum type="integer">0</ratings-sum>
          <ratings-count type="integer">0</ratings-count>
          <notes nil="true"/>
          <spoiler-flag type="boolean">false</spoiler-flag>
          <recommender-user-id1 type="integer">0</recommender-user-id1>
          <recommender-user-name1 nil="true"/>
          <work-id type="integer">14245059</work-id>
          <read-count nil="true"/>
          <last-comment-at type="datetime" nil="true"/>
          <started-at type="datetime" nil="true"/>
          <hidden-flag type="boolean">false</hidden-flag>
          <language-code type="integer" nil="true"/>
          <last-revision-at type="datetime">2015-12-29T21:25:34+00:00</last-revision-at>
          <non-friends-rating-count type="integer">0</non-friends-rating-count>
        </review>"""

        root = ETree.fromstring(r.content)
        review_id = root.findtext('id')
        permalink = 'https://www.goodreads.com/review/show/' + review_id
        resp_data['review-response'] = r.text

    if matched_categories:
        r = requests.post(ADD_BOOKS_TO_SHELVES_URL, data={
            'bookids': book_id,
            'shelves': ','.join(matched_categories),
        }, auth=auth)
        if r.status_code // 100 != 2:
            current_app.logger.error(
                'Failed to add book %s to additional shelves %r. Status: %s, Response: %r',
                book_id, matched_categories, r.status_code, r.text)
        resp_data['shelves-response'] = r.text

    return util.make_publish_success_response(permalink, data=resp_data)
