#!/bin/sh
# Put the latest dmrdash .deb in payload/, so the wizard copies it onto the card.
# Needs the GitHub CLI (gh) signed in while the dmrdash repo is private.
set -eu
cd "$(dirname "$0")"
rm -f payload/dmrdash_*_all.deb
gh release download --repo jcalado/dmrdash --pattern 'dmrdash_*_all.deb' --dir payload
ls payload/dmrdash_*_all.deb
