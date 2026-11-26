#!/usr/bin/env python3
"""
One-off Flickr OAuth login, to get the token and secret that flickr_favourites.py needs for
private photos. Asks for read permission only.

Usage:  pip install requests-oauthlib
        export FLICKR_API_KEY=yyyy FLICKR_API_SECRET=zzzz
        ./flickr_auth.py
Open the printed URL, click "OK, I'LL AUTHORIZE IT", copy the 9-digit code shown, paste it here.
It then prints the values to export.
"""
import os
import sys

from requests_oauthlib import OAuth1Session

BASE = "https://www.flickr.com/services/oauth/"

key, secret = os.environ.get("FLICKR_API_KEY"), os.environ.get("FLICKR_API_SECRET")
if not (key and secret):
    sys.exit("Set FLICKR_API_KEY and FLICKR_API_SECRET")

s = OAuth1Session(key, client_secret=secret, callback_uri="oob")
s.fetch_request_token(BASE + "request_token")
print("Open this URL and authorise:\n  " + s.authorization_url(BASE + "authorize", perms="read"))
verifier = input("Code shown by Flickr: ").strip()
tok = s.fetch_access_token(BASE + "access_token", verifier=verifier)

print(f"\nLogged in as {tok.get('fullname')} ({tok.get('user_nsid')}). Set these:\n")
print(f"export FLICKR_OAUTH_TOKEN={tok['oauth_token']}")
print(f"export FLICKR_OAUTH_SECRET={tok['oauth_token_secret']}")
print("\nThen run: ./flickr_favourites.py --flickr-user me")
