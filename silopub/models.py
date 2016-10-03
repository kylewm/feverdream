from silopub.ext import db
from sqlalchemy import func
import json
import urllib.parse
import datetime
import binascii
import os


class JsonType(db.TypeDecorator):
    """Represents an immutable structure as a json-encoded string.
    http://docs.sqlalchemy.org/en/rel_0_9/core/types.html#marshal-json-strings
    """
    impl = db.Text

    def process_bind_param(self, value, dialect):
        if value is not None:
            value = json.dumps(value)

        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            value = json.loads(value)
        return value


class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    service = db.Column(db.String(32))  # blogger, tumblr, wordpress, etc.
    sites = db.relationship('Site', backref='account',
                            cascade='all, delete, delete-orphan')

    # user-friendly slug for urls
    username = db.Column(db.String(256))
    # the id used to query apis
    user_id = db.Column(db.String(256))
    user_info = db.Column(JsonType)

    token = db.Column(db.String(512))
    token_secret = db.Column(db.String(512))
    refresh_token = db.Column(db.String(512))

    created = db.Column(db.DateTime)
    expiry = db.Column(db.DateTime)

    def update_sites(self, new_sites):
        for old_site in self.sites:
            for new_site in new_sites:
                if old_site.url == new_site.url:
                    new_site.silopub_tokens = old_site.silopub_tokens
        self.sites = new_sites

    def __repr__(self):
        return 'Account[service={}, username={}]'.format(
            self.service, self.username)

    @classmethod
    def lookup_by_user_id(cls, service_name, user_id):
        return cls.query.filter_by(
            service=service_name, user_id=user_id).first()


class Site(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey(Account.id))

    service = db.Column(db.String(32))  # blogger, tumblr, wordpress, etc.
    url = db.Column(db.String(512))
    # user-friendly slug for urls
    domain = db.Column(db.String(256))
    # the id used to query apis
    site_id = db.Column(db.String(256))
    site_info = db.Column(JsonType)

    # the connected service's OAuth access token and secret
    token = db.Column(db.String(512))
    token_secret = db.Column(db.String(512))

    # micropub access tokens we've produced
    silopub_tokens = db.relationship(
        'Token', backref='site',
        cascade='all, delete, delete-orphan')

    __mapper_args__ = {
        'polymorphic_on': service,
        'polymorphic_identity': 'unknown'
    }

    def edit_template_url(self):
        return None

    def edit_profile_url(self):
        return None

    def indieauth_url(self):
        return 'https://indieauth.com/auth?' + urllib.parse.urlencode({
            'me': self.url,
            'client_id': 'https://silo.pub/',
        })

    @classmethod
    def lookup_by_url(cls, url):
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc
        if parsed.path != '/':
            domain = domain + parsed.path

        return cls.query.filter(
            func.lower(Site.domain) == domain.lower()).first()

    def __repr__(self):
        return 'Site[domain={}]'.format(self.domain)


class Token(db.Model):
    token = db.Column(db.String(256), primary_key=True)
    issued_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime)
    site_id = db.Column(db.Integer, db.ForeignKey(Site.id))
    scope = db.Column(db.String)
    client_id = db.Column(db.String)

    @classmethod
    def create_or_update(cls, site, scope, client_id):
        # look for existing token
        now = datetime.datetime.utcnow()
        token = cls.query.filter_by(
            site=site, scope=scope, client_id=client_id).first()
        if token:
            token.updated_at = now
        else:
            token = cls(token=binascii.hexlify(os.urandom(16)).decode(),
                        site=site, scope=scope, client_id=client_id,
                        issued_at=now, updated_at=now)
            db.session.add(token)

        db.session.commit()
        return token

    def __repr__(self):
        return 'Token[{}]'.format(self.token)


class Blogger(Site):
    __mapper_args__ = {
        'polymorphic_identity': 'blogger'
    }

    def edit_profile_url(self):
        return 'https://www.blogger.com/edit-profile.g#widget.aboutme'

    def edit_template_url(self):
        return 'https://www.blogger.com/blogger.g?blogID={}#template'\
            .format(self.site_id)

    def __repr__(self):
        return 'Blogger[domain={}]'.format(self.domain)


class Tumblr(Site):
    __mapper_args__ = {
        'polymorphic_identity': 'tumblr'
    }

    def edit_template_url(self):
        return 'http://www.tumblr.com/customize/{}'.format(self.site_id)

    def edit_profile_url(self):
        return self.edit_template_url()

    def __repr__(self):
        return 'Tumblr[domain={}]'.format(self.domain)


class Wordpress(Site):
    __mapper_args__ = {
        'polymorphic_identity': 'wordpress'
    }

    def edit_template_url(self):
        return urllib.parse.urljoin(self.url, 'wp-admin/widgets.php')

    def edit_profile_url(self):
        return self.edit_template_url()

    def __repr__(self):
        return 'Wordpress[domain={}]'.format(self.domain)


class Twitter(Site):
    __mapper_args__ = {
        'polymorphic_identity': 'twitter'
    }

    def __repr__(self):
        return 'Twitter[username={}]'.format(self.site_id)


class Facebook(Site):
    __mapper_args__ = {
        'polymorphic_identity': 'facebook'
    }

    def __repr__(self):
        return 'Facebook[username={}]'.format(self.site_id)


class Flickr(Site):
    __mapper_args__ = {
        'polymorphic_identity': 'flickr'
    }

    def __repr__(self):
        return 'Flickr[username={}]'.format(self.site_id)


class GitHub(Site):
    __mapper_args__ = {
        'polymorphic_identity': 'github'
    }

    def __repr__(self):
        return 'GitHub[username={}]'.format(self.site_id)


class Goodreads(Site):
    __mapper_args__ = {
        'polymorphic_identity': 'goodreads'
    }

    def __repr__(self):
        return 'Goodreads[username={}]'.format(self.site_id)
