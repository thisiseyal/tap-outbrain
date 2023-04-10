#!/usr/bin/env python

from setuptools import setup, find_packages
import os.path

setup(name='tap-outbrain',
      version='0.4.9',
      description='Singer.io tap for extracting data from the Outbrain API',
      author='Eyal',
      url='https://singer.io',
      classifiers=['Programming Language :: Python :: 3 :: Only'],
      py_modules=['tap_outbrain'],
      install_requires=[
          "singer-python @ https://github.com/Aporia-LTD/singer-python/tarball/master#egg=package-5.13.1",
          "requests"
      ],
      entry_points='''
          [console_scripts]
          tap-outbrain=tap_outbrain:main
      ''',
      packages=find_packages(),
      package_data = {
        'tap_outbrain': ['schemas/*.json'],
      },
      include_package_data=True,
)
