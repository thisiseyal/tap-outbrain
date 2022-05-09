#!/usr/bin/env python3

from decimal import Decimal

import datetime
import json
import os
import time
import dateutil.parser

import backoff
import requests
import singer
import singer.requests
from singer import utils, metadata
from singer.catalog import Catalog, CatalogEntry
from singer.schema import Schema

import tap_outbrain.schemas as schemas

REQUIRED_CONFIG_KEYS = []
LOGGER = singer.get_logger()
SESSION = requests.Session()

BASE_URL = 'https://api.outbrain.com/amplify/v0.1'
CONFIG = {}

DEFAULT_STATE = {
    'campaign_performance': {}
}

DEFAULT_START_DATE = '2016-08-01'

# We can retrieve at most 2 campaigns per minute. We only have 5.5 hours
# to run so that works out to about 660 (120 campaigns per hour * 5.5 =
# 660) campaigns.
TAP_CAMPAIGN_COUNT_ERROR_CEILING = 660
MARKETERS_CAMPAIGNS_MAX_LIMIT = 50
# This is an arbitrary limit and can be tuned later down the road if we
# see need for it. (Tested with 200 at least)
REPORTS_MARKETERS_PERIODIC_MAX_LIMIT = 100

def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def load_schemas():
    """ Load schemas from schemas folder """
    schemas = {}
    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        with open(path) as file:
            schemas[file_raw] = Schema.from_dict(json.load(file))
    return schemas


def discover():
    raw_schemas = load_schemas()
    streams = []
    for stream_id, schema in raw_schemas.items():
        # TODO: populate any metadata and stream's key properties here..
        stream_metadata = []
        key_properties = []
        streams.append(
            CatalogEntry(
                tap_stream_id=stream_id,
                stream=stream_id,
                schema=schema,
                key_properties=key_properties,
                metadata=stream_metadata,
                replication_key=None,
                is_view=None,
                database=None,
                table=None,
                row_count=None,
                stream_alias=None,
                replication_method=None,
            )
        )
    return Catalog(streams)

@backoff.on_exception(backoff.constant,
                      (requests.exceptions.RequestException),
                      jitter=backoff.random_jitter,
                      max_tries=5,
                      giveup=singer.requests.giveup_on_http_4xx_except_429,
                      interval=30)
def request(url, access_token, params=None):
    # Optional query parameters
    if params is None:
        params = dict()

    LOGGER.info("Making request: GET {} {}".format(url, params))
    headers = {'OB-TOKEN-V1': access_token}
    if 'user_agent' in CONFIG:
        headers['User-Agent'] = CONFIG['user_agent']

    req = requests.Request('GET', url, headers=headers, params=params).prepare()
    LOGGER.info("GET {}".format(req.url))
    resp = SESSION.send(req)

    if resp.status_code >= 400:
        LOGGER.error("GET {} [{} - {}]".format(req.url, resp.status_code, resp.content))
        resp.raise_for_status()

    return resp


def generate_token(username, password):
    LOGGER.info("Generating new token using basic auth.")

    auth = requests.auth.HTTPBasicAuth(username, password)
    response = requests.get('{}/login'.format(BASE_URL), auth=auth)
    LOGGER.info("Got response code: {}".format(response.status_code))
    response.raise_for_status()

    return response.json().get('OB-TOKEN-V1')


def parse_datetime(date_time):
    parsed_datetime = dateutil.parser.parse(date_time)

    # the assumption is that the timestamp comes in in UTC
    return parsed_datetime.isoformat('T') + 'Z'


def parse_performance(result, extra_fields):
    metrics = result.get('metrics', {})
    metadata = result.get('metadata', {})

    to_return = {
        'fromDate': metadata.get('fromDate'),
        'impressions': int(metrics.get('impressions', 0)),
        'clicks': int(metrics.get('clicks', 0)),
        'ctr': float(metrics.get('ctr', 0.0)),
        'spend': float(metrics.get('spend', 0.0)),
        'ecpc': float(metrics.get('ecpc', 0.0)),
        'conversions': int(metrics.get('conversions', 0)),
        'conversionRate': float(metrics.get('conversionRate', 0.0)),
        'cpa': float(metrics.get('cpa', 0.0)),
    }
    to_return.update(extra_fields)

    return to_return


def get_date_ranges(start, end, interval_in_days):
    if start > end:
        return []

    to_return = []
    interval_start = start

    while interval_start < end:
        to_return.append({
            'from_date': interval_start,
            'to_date': min(end,
                           (interval_start + datetime.timedelta(
                               days=interval_in_days - 1)))
        })

        interval_start = interval_start + datetime.timedelta(
            days=interval_in_days)

    return to_return


def sync_campaign_performance(state, access_token, account_id, campaign_id):
    return sync_performance(
        state,
        access_token,
        account_id,
        'campaign_performance',
        campaign_id,
        {'campaignId': campaign_id},
        {'campaignId': campaign_id})


def sync_performance(state, access_token, account_id, table_name, state_sub_id,
                     extra_params, extra_persist_fields):
    """
    This function is heavily parameterized as it is used to sync performance
    both based on campaign ID alone, and by campaign ID and link ID.

    - `state`: state map
    - `access_token`: access token for Outbrain Amplify API
    - `account_id`: Outbrain marketer ID
    - `table_name`: the table name to use. At present:
      `campaign_performance`
    - `state_sub_id`: the id to use within the state map to identify this
                      sub-object. For example,

                        state['campaign_performance'][state_sub_id]

                      is used for the `campaign_performance` table.
    - `extra_params`: extra params sent to the Outbrain API
    - `extra_persist_fields`: extra fields pushed into the destination data.
                              For example:

                                {'campaignId': '000b...'}
    """
    # sync 2 days before last saved date, or DEFAULT_START_DATE
    from_date = datetime.datetime.strptime(
        state.get(table_name, {})
            .get(state_sub_id, DEFAULT_START_DATE),
        '%Y-%m-%d').date() - datetime.timedelta(days=2)

    to_date = datetime.date.today()

    interval_in_days = REPORTS_MARKETERS_PERIODIC_MAX_LIMIT

    date_ranges = get_date_ranges(from_date, to_date, interval_in_days)

    last_request_start = None

    for date_range in date_ranges:
        LOGGER.info(
            'Pulling {} for {} from {} to {}'
                .format(table_name,
                        extra_persist_fields,
                        date_range.get('from_date'),
                        date_range.get('to_date')))

        params = {
            'from': date_range.get('from_date'),
            'to': date_range.get('to_date'),
            'breakdown': 'daily',
            'limit': REPORTS_MARKETERS_PERIODIC_MAX_LIMIT,
            'sort': '+fromDate',
            'includeArchivedCampaigns': True,
        }
        params.update(extra_params)

        last_request_start = utils.now()
        response = request(
            '{}/reports/marketers/{}/periodic'.format(BASE_URL, account_id),
            access_token,
            params).json()
        if REPORTS_MARKETERS_PERIODIC_MAX_LIMIT < response.get('totalResults'):
            LOGGER.warn(
                'More performance data (`{}`) than the tap can currently retrieve (`{}`)'.format(
                    response.get('totalResults'), REPORTS_MARKETERS_PERIODIC_MAX_LIMIT))
        else:
            LOGGER.info(
                'Syncing `{}` rows of performance data for campaign `{}`. Requested `{}`.'.format(
                    response.get('totalResults'), state_sub_id,
                    REPORTS_MARKETERS_PERIODIC_MAX_LIMIT))
        last_request_end = utils.now()

        LOGGER.info('Done in {} sec'.format(
            last_request_end.timestamp() - last_request_start.timestamp()))

        performance = [
            parse_performance(result, extra_persist_fields)
            for result in response.get('results')]

        for record in performance:
            singer.write_record(table_name, record, time_extracted=last_request_end)

        last_record = performance[-1]
        new_from_date = last_record.get('fromDate')

        state[table_name][state_sub_id] = new_from_date
        singer.write_state(state)

        from_date = new_from_date

        if last_request_start is not None and \
                (time.time() - last_request_end.timestamp()) < 30:
            to_sleep = 30 - (time.time() - last_request_end.timestamp())
            LOGGER.info(
                'Limiting to 2 requests per minute. Sleeping {} sec '
                'before making the next reporting request.'
                    .format(to_sleep))
            time.sleep(to_sleep)


def parse_campaign(campaign):
    if campaign.get('budget') is not None:
        campaign['budget']['creationTime'] = parse_datetime(
            campaign.get('budget').get('creationTime'))
        campaign['budget']['lastModified'] = parse_datetime(
            campaign.get('budget').get('lastModified'))

    return campaign


def get_campaigns_page(account_id, access_token, offset):
    # NOTE: We probably should be more aggressive about ensuring that the
    # response was successful.
    return request(
        '{}/marketers/{}/campaigns'.format(BASE_URL, account_id),
        access_token, {'limit': MARKETERS_CAMPAIGNS_MAX_LIMIT,
                       'offset': offset}).json()


def get_campaign_pages(account_id, access_token):
    more_campaigns = True
    offset = 0

    while more_campaigns:
        LOGGER.info('Retrieving campaigns from offset `{}`'.format(
            offset))
        campaign_page = get_campaigns_page(account_id, access_token,
                                           offset)
        if TAP_CAMPAIGN_COUNT_ERROR_CEILING < campaign_page.get('totalCount'):
            msg = 'Tap found `{}` campaigns which is more than can be retrieved in the alloted time (`{}`).'.format(
                campaign_page.get('totalCount'), TAP_CAMPAIGN_COUNT_ERROR_CEILING)
            LOGGER.error(msg)
            raise Exception(msg)
        LOGGER.info('Retrieved offset `{}` campaigns out of `{}`'.format(
            offset, campaign_page.get('totalCount')))
        yield campaign_page
        if (offset + MARKETERS_CAMPAIGNS_MAX_LIMIT) < campaign_page.get('totalCount'):
            offset += MARKETERS_CAMPAIGNS_MAX_LIMIT
        else:
            more_campaigns = False

    LOGGER.info('Finished retrieving `{}` campaigns'.format(
        campaign_page.get('totalCount')))


def sync_campaign_page(state, access_token, account_id, campaign_page):
    campaigns = [parse_campaign(campaign) for campaign
                 in campaign_page.get('campaigns', [])]

    for campaign in campaigns:
        singer.write_record('campaigns', campaign,
                            time_extracted=utils.now())
        sync_campaign_performance(state, access_token, account_id,
                                  campaign.get('id'))


def sync_campaigns(state, access_token, account_id):
    LOGGER.info('Syncing campaigns.')

    for campaign_page in get_campaign_pages(account_id, access_token):
        sync_campaign_page(state, access_token, account_id, campaign_page)

    LOGGER.info('Done!')


def parse_marketer(marketer):
    return {
        'id': str(marketer['id']),
        'name': str(marketer['name']),
        'enabled': bool(marketer['enabled']),
        'currency': str(marketer['currency']),
        'creationTime': parse_datetime(marketer['creationTime']),
        'lastModified': parse_datetime(marketer['lastModified']),
        'blockedSites': str(marketer['blockedSites']),
        'useFirstPartyCookie': bool(marketer['useFirstPartyCookie']),
    }


def get_marketers(account_id, access_token):
    """
    Retrieve all Marketers associated with the current user
    """

    url = '{}/marketers'.format(BASE_URL, account_id)

    marketers = request(url, access_token).json()['marketers']

    LOGGER.info('Retrieved %s marketers', len(marketers))

    return marketers


def sync_marketers(access_token, account_id):
    LOGGER.info('Syncing marketers.')

    # Retrieve account data
    marketers = get_marketers(account_id, access_token)

    # Parse data types
    marketers = map(parse_marketer, marketers)

    # Emit rows
    for marketer in marketers:
        singer.write_record('marketers', marketer, time_extracted=utils.now())

    LOGGER.info('Done!')

    return marketers


def sync(config, state = None, catalog = None):
    # pylint: disable=global-statement
    global DEFAULT_START_DATE
    if not state:
        state = DEFAULT_STATE

    with open(config) as config_file:
        config = json.load(config_file)
        CONFIG.update(config)

    missing_keys = []
    if 'username' not in config:
        missing_keys.append('username')
    else:
        username = config['username']

    if 'password' not in config:
        missing_keys.append('password')
    else:
        password = config['password']

    if 'account_id' not in config:
        missing_keys.append('account_id')
    else:
        account_id = config['account_id']

    if 'start_date' not in config:
        missing_keys.append('start_date')
    else:
        # only want the date
        DEFAULT_START_DATE = config['start_date'][:10]

    if missing_keys:
        LOGGER.fatal("Missing {}.".format(", ".join(missing_keys)))
        raise RuntimeError

    access_token = config.get('access_token')

    if access_token is None:
        access_token = generate_token(username, password)

    if access_token is None:
        LOGGER.fatal("Failed to generate a new access token.")
        raise RuntimeError

    # NEVER RAISE THIS ABOVE DEBUG!
    LOGGER.debug('Using access token `{}`'.format(access_token))

    # for stream in catalog.get_selected_streams(state):
    #     LOGGER.info("Syncing stream:" + stream.tap_stream_id)
    LOGGER.info(f'Writing schemas and starting full sync..')

    singer.write_schema('marketers', schemas.marketer, key_properties=['id'])
    singer.write_schema('campaigns',
                        schemas.campaign,
                        key_properties=["id"])
    singer.write_schema('campaign_performance',
                        schemas.campaign_performance,
                        key_properties=["campaignId", "fromDate"],
                        bookmark_properties=["fromDate"])

    # Retrieve all accounts that the authenticated account has access to
    marketers = sync_marketers(access_token, account_id)

    # Iterate over all these customer accounts
    for marketer in marketers:
        sync_campaigns(state, access_token, marketer['id'])

@utils.handle_top_exception(LOGGER)
def main():
    # Parse command line arguments
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    # If discover flag was passed, run discovery mode and dump output to stdout
    if args.discover:
        catalog = discover()
        catalog.dump()
    # Otherwise run in sync mode
    else:
        if args.catalog:
            catalog = args.catalog
        else:
            catalog = discover()
        sync(args.config, args.state, catalog)


if __name__ == "__main__":
    main()