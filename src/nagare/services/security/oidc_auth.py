# Encoding: utf-8

# --
# Copyright (c) 2008-2024 Net-ng.
# All rights reserved.
#
# This software is licensed under the BSD License, as described in
# the file LICENSE.txt, which you should have received as part of
# this distribution.
# --

import os
import copy
import time
import threading
from base64 import urlsafe_b64decode, urlsafe_b64encode

import requests
from jose import JOSEError, jwk, jws, jwt, constants

from nagare import log, partial
from nagare.renderers import xml
from nagare.services.security import cookie_auth


class Login(xml.Renderable):
    ACTION_PRIORITY = 5

    def __init__(self, manager, renderer, scopes, location):
        self.manager = manager
        self.renderer = renderer
        self.scopes = scopes
        self.location = location

        self._action = None
        self.with_request = False
        self.args = ()
        self.kw = {}

    @partial.max_number_of_args(2)
    def action(self, action, args, with_request=False, **kw):
        self._action = action
        self.with_request = with_request
        self.args = args
        self.kw = kw

        return self

    def set_sync_action(self, action_id, params):
        pass

    set_async_action = set_sync_action

    def render(self, h):
        if self._action is not None:
            action_id, _ = self.renderer.register_callback(
                self, self.ACTION_PRIORITY, self._action, self.with_request, *self.args, **self.kw
            )
        else:
            action_id = None

        _, url, params, _ = self.manager.create_auth_request(
            h.session_id, h.state_id, action_id, h.request.create_redirect_url(self.location), self.scopes
        )

        redirection = h.request.create_redirect_response(url, add_slash=False, **params)

        response = h.response
        response.status_code = redirection.status_code
        response.headers = redirection.headers

        return response


class Authentication(cookie_auth.Authentication):
    LOAD_PRIORITY = cookie_auth.Authentication.LOAD_PRIORITY + 1

    REQUIRED_ENDPOINTS = {'authorization_endpoint', 'token_endpoint'}
    ENDPOINTS = REQUIRED_ENDPOINTS | {'discovery_endpoint', 'userinfo_endpoint', 'end_session_endpoint'}
    EXCLUDED_CLAIMS = {'iss', 'aud', 'exp', 'iat', 'auth_time', 'nonce', 'acr', 'amr', 'azp'} | {
        'session_state',
        'typ',
        'nbf',
    }

    CONFIG_SPEC = dict(
        copy.deepcopy(cookie_auth.Authentication.CONFIG_SPEC),
        host='string(default="localhost", help="server hostname")',
        port='integer(default=None, help="server port")',
        ssl='boolean(default=True, help="HTTPS protocol")',
        proxy='string(default=None, help="HTTP/S proxy to use")',
        verify='boolean(default=True, help="SSL certificate verification")',
        timeout='integer(default=5, help="communication timeout")',
        client_id='string(help="application identifier")',
        client_secret='string(default="", help="application authentication")',  # noqa: S106
        secure='boolean(default=True, help="JWT signature verification")',
        algorithms='string_list(default=list({}), help="accepted signing/encryption algorithms")'.format(
            ', '.join('"%s"' % algo for algo in constants.ALGORITHMS.SUPPORTED if algo.isupper())
        ),
        key='string(default=None, help="cookie encoding key")',
        jwks_uri='string(default=None, help="JWK keys set document")',
        issuer='string(default=None, help="server identifier")',
        time_skew='float(default=0, help="Acceptable time skew with the issuer, in seconds")',
    )
    CONFIG_SPEC['cookie']['activated'] = 'boolean(default=False)'
    CONFIG_SPEC['cookie']['encrypt'] = 'boolean(default=False)'
    CONFIG_SPEC.update({endpoint: 'string(default=None)' for endpoint in ENDPOINTS})

    def __init__(
        self,
        name,
        dist,
        client_id,
        client_secret='',  # noqa: S107
        secure=True,
        algorithms=constants.ALGORITHMS.SUPPORTED,
        host='localhost',
        port=None,
        ssl=True,
        verify=True,
        timeout=5,
        proxy=None,
        key=None,
        jwks_uri=None,
        issuer=None,
        time_skew=0,
        services_service=None,
        **config,
    ):
        services_service(
            super(Authentication, self).__init__,
            name,
            dist,
            client_id=client_id,
            client_secret=client_secret,
            secure=secure,
            algorithms=algorithms,
            host=host,
            port=port,
            ssl=ssl,
            verify=verify,
            timeout=timeout,
            proxy=proxy,
            key=key,
            jwks_uri=jwks_uri,
            issuer=issuer,
            time_skew=time_skew,
            **config,
        )
        key = urlsafe_b64decode(key) if key else os.urandom(32)
        self.jwk_key = jwk.construct(key, 'HS256')
        self.key = urlsafe_b64encode(key).decode('ascii')

        self.timeout = timeout
        self.client_id = client_id
        self.client_secret = client_secret
        self.secure = secure
        self.algorithms = algorithms
        self.verify = verify
        self.proxies = {'http': proxy, 'https': proxy} if proxy else None

        self.issuer = issuer
        self.jwks_uri = jwks_uri
        self.jwks_expiration = None
        self.jwks_lock = threading.Lock()
        self.signing_keys = {}
        self.time_skew = time_skew

        self.ident = name

        if not port:
            port = 443 if ssl else 80

        endpoint_params = dict(
            config,
            scheme='https' if ssl else 'http',
            host=host,
            port=port,
            base_url='{}://{}:{}'.format(('https' if ssl else 'http'), host, port),
        )

        self.endpoints = {endpoint: (config[endpoint] or '').format(**endpoint_params) for endpoint in self.ENDPOINTS}

    def send_request(self, method, url, params=None, data=None):
        r = requests.request(
            method,
            url,
            params=params or {},
            data=data or {},
            verify=self.verify,
            timeout=self.timeout,
            proxies=self.proxies,
        )
        r.raise_for_status()
        return r

    def create_discovery_request(self):
        discovery_endpoint = self.endpoints['discovery_endpoint']

        return (None, None, None, None) if discovery_endpoint is None else ('GET', discovery_endpoint, {}, {})

    def create_auth_request(self, session_id, state_id, action_id, redirect_url, scopes=(), **params):
        state = b'%d#%d#%s' % (session_id, state_id, (action_id or '').encode('ascii'))

        params = dict(
            {
                'response_type': 'code',
                'client_id': self.client_id,
                'redirect_uri': redirect_url,
                'scope': ' '.join({'openid'} | set(scopes)),
                'access_type': 'offline',
                'state': '#{}#{}'.format(self.ident, self.encrypt(state).decode('ascii')),
            },
            **params,
        )

        return 'GET', self.endpoints['authorization_endpoint'], params, {}

    def create_token_request(self, redirect_url, code):
        payload = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_url,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }

        return 'POST', self.endpoints['token_endpoint'], {}, payload

    def create_refresh_token_request(self, refresh_token):
        payload = {
            'grant_type': 'refresh_token',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': refresh_token,
        }

        return 'POST', self.endpoints['token_endpoint'], {}, payload

    def create_end_session_request(self, refresh_token):
        payload = {'client_id': self.client_id, 'client_secret': self.client_secret, 'refresh_token': refresh_token}

        return 'POST', self.endpoints['end_session_endpoint'], {}, payload

    def create_userinfo_request(self, access_token):
        return 'POST', self.endpoints.get('userinfo_endpoint'), {}, {'access_token': access_token}

    def fetch_keys(self):
        if self.jwks_uri and self.jwks_expiration and (time.time() > self.jwks_expiration):
            with self.jwks_lock:
                logger = log.get_logger('.keys', self.logger)
                certs = self.send_request('GET', self.jwks_uri)

                new_keys = certs.json().get('keys', [])
                new_keys_id = {key['kid'] for key in new_keys}
                if new_keys_id != set(self.signing_keys):
                    logger.debug(
                        'New signing keys fetched: {} -> {}'.format(sorted(self.signing_keys), sorted(new_keys_id))
                    )
                    self.signing_keys = {key.pop('kid', None): key for key in new_keys}
                else:
                    logger.debug('Same signing keys fetched: {}'.format(sorted(self.signing_keys)))

                cache_controls = [v.split('=') for v in certs.headers['Cache-Control'].split(',') if '=' in v]
                cache_controls = {k.strip(): v.strip() for k, v in cache_controls}
                max_age = cache_controls.get('max-age')

                if max_age and max_age.isdigit():
                    logger.debug('Signing keys max age: {}'.format(max_age))
                    self.jwks_expiration = time.time() + int(max_age)
                else:
                    logger.debug('No expiration date for signing keys')
                    self.jwks_expiration = None

    def handle_start(self, app, oidc_listener_service):
        oidc_listener_service.register_service(self.ident, self)

        method, url, params, data = self.create_discovery_request()
        if url:
            r = self.send_request(method, url, params, data).json()

            self.issuer = r['issuer']
            self.endpoints = {endpoint: r.get(endpoint) for endpoint in self.ENDPOINTS}
            jwks_uri = r.get('jwks_uri')
            if jwks_uri and not self.jwks_uri:
                self.jwks_uri = jwks_uri

        self.jwks_expiration = time.time() - 1

        missing_endpoints = [endpoint for endpoint in self.REQUIRED_ENDPOINTS if not self.endpoints[endpoint]]
        if missing_endpoints:
            self.logger.error('Endpoints without values: ' + ', '.join(missing_endpoints))

        self.fetch_keys()

    def handle_request(self, chain, **params):
        self.fetch_keys()
        return super(Authentication, self).handle_request(chain, **params)

    def refresh_token(self, refresh_token):
        method, url, params, data = self.create_refresh_token_request(refresh_token)
        return self.send_request(method, url, params, data)

    def is_auth_response(self, request):
        code, session_id, state_id, action_id = None, 0, 0, ''

        state = request.params.get('state', '')
        code = request.params.get('code')

        if code and state.startswith('#'):
            state = state.rsplit('#', 1)[1]
            try:
                state = self.decrypt(state.encode('ascii')).decode('ascii')
                session_id, state_id, action_id = state.split('#')
            except cookie_auth.InvalidToken:
                code = None

        return code, int(session_id), int(state_id), action_id

    def to_cookie(self, **credentials):
        credentials = self.filter_credentials(credentials, {'sub'})

        if self.encrypted:
            cookie = super(Authentication, self).to_cookie(credentials.pop('sub'), **credentials)
        else:
            cookie = jwt.encode(credentials, self.jwk_key, 'HS256')

        return cookie

    def from_cookie(self, cookie, max_age):
        if self.encrypted:
            principal, credentials = super(Authentication, self).from_cookie(cookie, max_age)
            credentials['sub'] = principal
        else:
            credentials = jwt.decode(cookie.decode('ascii'), self.jwk_key, 'HS256')
            credentials = self.filter_credentials(credentials, {'sub'})

        return credentials['sub'], credentials

    def retrieve_credentials(self, session):
        if self.cookie or not session:
            return None, {}

        credentials = session.get('nagare.credentials', {})
        return credentials.get('sub'), credentials

    @staticmethod
    def filter_credentials(credentials, to_keep):
        return {k: v for k, v in credentials.items() if k in to_keep}

    def store_credentials(self, session, credentials):
        if not self.cookie and session:
            session['nagare.credentials'] = self.filter_credentials(credentials, {'sub'})

    def request_credentials(self, request, code, action_id):
        credentials = {}

        method, url, params, data = self.create_token_request(request.create_redirect_url(), code)
        response = self.send_request(method, url, params, data)
        if response.status_code == 400:
            error = response.text
            if response.headers.get('content-type') == 'application/json':
                response = response.json()
                if 'error' in response:
                    error = response['error']
                    description = response.get('error_description')
                    if description:
                        error += ': ' + description
            self.logger.error(error)
        elif response.status_code != 200:
            self.logger.error('Authentication error')
        else:
            tokens = response.json()
            id_token = tokens['id_token']

            try:
                headers = jws.get_unverified_header(id_token)
                key = self.signing_keys.get(headers.get('kid'), self.signing_keys)

                credentials = jwt.decode(
                    id_token,
                    key,
                    self.algorithms if self.secure else None,
                    {
                        'verify_iss': self.issuer is not None,
                        'verify_at_hash': 'access_token' in tokens,
                        'leeway': self.time_skew,
                    },
                    audience=self.client_id,
                    issuer=self.issuer,
                    access_token=tokens.get('access_token'),
                )
            except JOSEError as e:
                self.logger.error('Invalid id_token: ' + e.args[0])
            else:
                credentials['access_token'] = tokens['access_token']
                refresh_token = tokens.get('refresh_token')
                if refresh_token is not None:
                    credentials['refresh_token'] = refresh_token

                if action_id:
                    request.environ['QUERY_STRING'] = action_id + '='

        return credentials

    def get_principal(self, request, response, session, session_id, state_id, **params):
        new_response = None
        credentials = {}

        code, _, _, action_id = self.is_auth_response(request)
        if code:
            credentials = self.request_credentials(request, code, action_id)
            if credentials:
                new_response = request.create_redirect_response(response=response, _s=session_id, _c='%05d' % state_id)

        if not credentials:
            principal, credentials = self.retrieve_credentials(session)
            if not principal:
                principal, credentials, r = super(Authentication, self).get_principal(
                    request=request, response=response, **params
                )

        if credentials:
            self.store_credentials(session, credentials)

        return credentials.get('sub'), credentials, new_response

    def login(self, h, scopes=(), location=None):
        return Login(self, h, scopes, location)

    def logout(self, location='', delete_session=True, user=None, access_token=None):
        """Disconnection of the current user.

        Mark the user object as expired

        In:
          - ``location`` -- location to redirect to
          - ``delete_session`` -- is the session expired too?
        """
        status = super(Authentication, self).logout(location, delete_session, user)

        if access_token is not None:
            method, url, params, data = self.create_end_session_request(access_token)
            if url:
                response = self.send_request(method, url, params, data)
                status = status and (response.status_code == 204)

        return status

    def user_info(self, access_token):
        method, url, params, data = self.create_userinfo_request(access_token)
        if not url:
            return {}

        response = self.send_request(method, url, params, data)
        return response.json() if response.status_code == 200 else {}


class AuthenticationWithDiscovery(Authentication):
    CONFIG_SPEC = dict(
        Authentication.CONFIG_SPEC, discovery_endpoint='string(default="{base_url}/.well-known/openid-configuration")'
    )


# ---------------------------------------------------------------------------------------------------------------------


class KeycloakAuthentication(Authentication):
    CONFIG_SPEC = dict(
        Authentication.CONFIG_SPEC,
        realm='string',
        discovery_endpoint='string(default="{base_url}/auth/realms/{realm}/.well-known/openid-configuration")',
    )


class GoogleAuthentication(AuthenticationWithDiscovery):
    CONFIG_SPEC = dict(AuthenticationWithDiscovery.CONFIG_SPEC, host='string(default="accounts.google.com")')


class AzureAuthentication(Authentication):
    CONFIG_SPEC = dict(
        AuthenticationWithDiscovery.CONFIG_SPEC,
        host='string(default="login.microsoftonline.com")',
        discovery_endpoint='string(default="{base_url}/{tenant}/v2.0/.well-known/openid-configuration")',
        tenant='string(default="common")',
    )
