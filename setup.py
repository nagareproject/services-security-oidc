# Encoding: utf-8

# --
# Copyright (c) 2008-2021 Net-ng.
# All rights reserved.
#
# This software is licensed under the BSD License, as described in
# the file LICENSE.txt, which you should have received as part of
# this distribution.
# --

import os
import sys
import subprocess

from setuptools import setup, find_packages

try:
    import stackless  # noqa: F401
    has_stackless = True
except ImportError:
    has_stackless = False

try:
    import lxml  # noqa: F401
    has_lxml = True
except ImportError:
    has_lxml = False

if has_stackless and not has_lxml:
    # Under Stackless Python or PyPy, the pre-compiled lxml wheel ends with a segfault
    subprocess.check_call([sys.executable] + ' -m pip install --no-binary :all: lxml'.split())


with open(os.path.join(os.path.dirname(__file__), 'README.rst')) as description:
    LONG_DESCRIPTION = description.read()

setup(
    name='nagare-services-security-oidc',
    author='Net-ng',
    author_email='alain.poirier@net-ng.com',
    description='Nagare OpenID Connect security service',
    long_description=LONG_DESCRIPTION,
    license='BSD',
    keywords='',
    url='https://github.com/nagareproject/services-security-oidc',
    packages=find_packages(),
    zip_safe=False,
    setup_requires=['setuptools_scm'],
    use_scm_version=True,
    install_requires=[
        'python-jwt',
        'nagare-partial',
        'nagare-services',
        'nagare-services-security',
        'nagare-services-security-oidc-listener',
        'nagare-renderers-xml'
    ]
)
