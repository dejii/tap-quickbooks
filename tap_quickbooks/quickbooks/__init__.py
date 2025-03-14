import re
import json
import argparse
import threading
import time
import backoff
import requests
from urllib.parse import urlparse
from google.cloud import storage
from requests.exceptions import RequestException
import singer
import singer.utils as singer_utils
import os;
from pathlib import Path
from typing import Dict
from singer import metadata, metrics
from tap_quickbooks.quickbooks.reportstreams.MonthlyBalanceSheetReport import MonthlyBalanceSheetReport
from tap_quickbooks.quickbooks.reportstreams.ProfitAndLossDetailReport import ProfitAndLossDetailReport
from tap_quickbooks.quickbooks.reportstreams.BalanceSheetReport import BalanceSheetReport
from tap_quickbooks.quickbooks.reportstreams.GeneralLedgerAccrualReport import GeneralLedgerAccrualReport
from tap_quickbooks.quickbooks.reportstreams.GeneralLedgerCashReport import GeneralLedgerCashReport
from tap_quickbooks.quickbooks.reportstreams.CashFlowReport import CashFlowReport
from tap_quickbooks.quickbooks.reportstreams.DailyCashFlowReport import DailyCashFlowReport
from tap_quickbooks.quickbooks.reportstreams.MonthlyCashFlowReport import MonthlyCashFlowReport
from tap_quickbooks.quickbooks.reportstreams.TransactionListReport import TransactionListReport

from tap_quickbooks.quickbooks.rest import Rest
from tap_quickbooks.quickbooks.exceptions import (
    TapQuickbooksException,
    TapQuickbooksQuotaExceededException)

LOGGER = singer.get_logger()

# The minimum expiration setting for SF Refresh Tokens is 15 minutes
REFRESH_TOKEN_EXPIRATION_PERIOD = 900

REST_API_TYPE = "REST"


def log_backoff_attempt(details):
    LOGGER.info("ConnectionError detected, triggering backoff: %d try", details.get("tries"))


def _get_abs_path(path: str) -> str:
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def _load_object_definitions() -> Dict:
    '''Loads a JSON schema file for a given
    Quickbooks Report resource into a dict representation.
    '''
    schema_path = _get_abs_path("schemas")
    return singer.utils.load_json(f"{schema_path}/object_definition.json")


def read_json_file(filename):
    # read file
    with open(f"{filename}", 'r') as filetoread:
        data = filetoread.read()

    # parse file
    content = json.loads(data)

    return content

def extract_bucket_name(uri: str) -> str:
    parsed_uri = urlparse(uri)
    if parsed_uri.scheme != 'gs':
        raise Exception(f"Invalid GCS URI: {uri}")
    bucket_name = parsed_uri.netloc
    return bucket_name

def read_json_file_gcs(gcs_auth_state_uri):
    LOGGER.info(f"Reading auth state from GCS: {gcs_auth_state_uri}")
    bucket_name = extract_bucket_name(gcs_auth_state_uri)
    filename = urlparse(gcs_auth_state_uri).path.lstrip('/')
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(filename)
    try:
        content = blob.download_as_string()
        return json.loads(content)
    except Exception as e:
        LOGGER.info(f"Failed to read auth state from GCS: {gcs_auth_state_uri} - {str(e)}")
        return {}

def write_json_file_gcs(auth_state, gcs_auth_state_uri):
    LOGGER.info(f"Writing auth state to GCS: {gcs_auth_state_uri}")
    bucket_name = extract_bucket_name(gcs_auth_state_uri)
    filename = urlparse(gcs_auth_state_uri).path.lstrip('/')
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(filename)
    blob.upload_from_string(json.dumps(auth_state), content_type='application/json')
    

def write_json_file(filename, content):
    with open(filename, 'w') as f:
        json.dump(content, f, indent=4)


def get_auth_state_file():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', help='Config file', required=True)
    _args, unknown = parser.parse_known_args()
    config_file = _args.config
    # .meltano/run/tap-quickbooks/config.json => .meltano/extractors/tap-quickbooks/auth_state.json
    auth_state_file = Path(config_file).parent.parent.parent.joinpath('extractors/tap-quickbooks/auth_state.json').absolute()
    return auth_state_file


def read_auth_state(gcs_auth_state_uri=None):
    if gcs_auth_state_uri:
        auth_state = read_json_file_gcs(gcs_auth_state_uri)
        return auth_state
    else: # fall back to local
        auth_state_file = get_auth_state_file()
        try:
            auth_state = read_json_file(auth_state_file)
        except FileNotFoundError:
            auth_state = {}
        return auth_state


def write_auth_state(auth_state, gcs_auth_state_uri=None):
    if gcs_auth_state_uri:
        write_json_file_gcs(auth_state, gcs_auth_state_uri)
    else:
        auth_state_file = get_auth_state_file()
        write_json_file(auth_state_file, auth_state)


QB_OBJECT_DEFINITIONS = _load_object_definitions()
QB_OBJECTS = QB_OBJECT_DEFINITIONS.keys()


def field_to_property_schema(field, mdata):  # pylint:disable=too-many-branches

    number_type = {
        "type": [
            "null",
            "number"
        ]
    }

    string_type = {
        "type": [
            "string",
            "null"
        ]
    }

    boolean_type = {
        "type": [
            "boolean",
            "null"
        ]
    }

    datetime_type = {
        "anyOf": [
            {
                "type": "string",
                "format": "date-time"
            },
            string_type
        ]
    }

    object_type = {
        "type": [
            "null",
            "object"
        ]
    }

    array_type = {
        "type": ["null", "array"]
    }

    ref_type = {
        "type": object_type["type"],
        "properties": {
            "value": string_type,
            "name": string_type,
        }
    }

    qb_types = {
        "number": number_type,
        "string": string_type,
        "datetime": datetime_type,
        "object": object_type,
        "array": array_type,
        "boolean": boolean_type,
        "object_reference": string_type,
        "email": string_type,
        "address": string_type,
        "metadata": string_type,
        "ref_type": ref_type
    }

    qb_types["custom_field"] = {
        "type": object_type["type"],
        "properties": {
            "DefinitionId": string_type,
            "Name": string_type,
            "Type": string_type,
            "StringValue": string_type
        }
    }

    qb_types["invoice_line"] = {
        "type": object_type["type"],
        "properties": {
            "Id": string_type,
            "LineNum": string_type,
            "Amount": number_type,
            "DetailType": string_type,
            "Description": string_type,
            "SalesItemLineDetail": {
                "type": object_type["type"],
                "properties": {
                    "ItemRef": qb_types["ref_type"],
                    "ClassRef": qb_types["ref_type"],
                    "ItemAccountRef": qb_types["ref_type"],
                    "TaxCodeRef": qb_types["ref_type"],
                    "Qty": number_type,
                    "UnitPrice": number_type,
                    "ServiceDate": qb_types["datetime"],
                    "Description" : string_type
                }
            },
            "SubTotalLineDetail": {
                "type": object_type["type"],
                "properties": {
                    "ItemRef": qb_types["ref_type"]
                }
            },
            "DiscountLineDetail": {
                "type": object_type["type"],
                "properties": {
                    "DiscountAccountRef": qb_types["object_reference"],
                    "DiscountPercent": number_type
                }
            },
            "DescriptionLineDetail": {
                "type": object_type["type"],
                "properties": {
                    "TaxCodeRef": qb_types["object_reference"],
                    "ServiceDate": qb_types["datetime"]
                }
            }
        }
    }

    qb_types["journal_entry_line"] = {
        "type": object_type["type"],
        "properties": {
            "Id": string_type,
            "Description": string_type,
            "Amount": number_type,
            "DetailType": string_type,
            "JournalEntryLineDetail": {
                "type": object_type["type"],
                "properties": {
                    "PostingType": string_type,
                    "Entity": {
                        "type": object_type["type"],
                        "properties": {
                            "Type": string_type,
                            "EntityRef": qb_types["object_reference"]
                        }
                    },
                    "AccountRef": qb_types["object_reference"],
                    "ClassRef": qb_types["object_reference"],
                    "DepartmentRef": qb_types["object_reference"]
                }
            }
        }
    }

    qb_type = field['type']
    property_schema = qb_types[qb_type]
    if qb_type == 'array':
        property_schema["items"] = qb_types[field['child_type']]

    return property_schema, mdata


class Quickbooks():
    # pylint: disable=too-many-instance-attributes,too-many-arguments
    def __init__(self,
                 refresh_token=None,
                 token=None,
                 qb_client_id=None,
                 qb_client_secret=None,
                 quota_percent_per_run=None,
                 quota_percent_total=None,
                 is_sandbox=None,
                 gcs_auth_state_uri=None,
                 include_deleted = None,
                 select_fields_by_default=None,
                 default_start_date=None,
                 api_type=None,
                 report_period_days = None,
                 gl_full_sync = None,
                 gl_weekly = None,
                 gl_daily = None,
                 gl_basic_fields = None,
                 realm_id=None):
        self.api_type = api_type.upper() if api_type else None
        self.report_period_days = report_period_days
        self.gl_full_sync = gl_full_sync
        self.gl_weekly = gl_weekly
        self.gl_daily = gl_daily
        self.gl_basic_fields = gl_basic_fields
        self.include_deleted = include_deleted
        self.realm_id = realm_id
        self.refresh_token = refresh_token
        self.token = token
        self.qb_client_id = qb_client_id
        self.gcs_auth_state_uri = gcs_auth_state_uri
        self.qb_client_secret = qb_client_secret
        self.session = requests.Session()
        self.access_token = None

        self.base_url = "https://sandbox-quickbooks.api.intuit.com/v3/company/" if is_sandbox is True else 'https://quickbooks.api.intuit.com/v3/company/'

        self.instance_url = f"{self.base_url}{realm_id}"

        LOGGER.info(f"Instance URL :- {self.instance_url}")

        if isinstance(quota_percent_per_run, str) and quota_percent_per_run.strip() == '':
            quota_percent_per_run = None
        if isinstance(quota_percent_total, str) and quota_percent_total.strip() == '':
            quota_percent_total = None
        self.quota_percent_per_run = float(
            quota_percent_per_run) if quota_percent_per_run is not None else 25
        self.quota_percent_total = float(
            quota_percent_total) if quota_percent_total is not None else 80
        self.is_sandbox = is_sandbox is True or (isinstance(is_sandbox, str) and is_sandbox.lower() == 'true')
        self.select_fields_by_default = select_fields_by_default is True or (
                isinstance(select_fields_by_default, str) and select_fields_by_default.lower() == 'true')
        self.default_start_date = default_start_date
        self.rest_requests_attempted = 0
        self.jobs_completed = 0
        self.login_timer = None
        self.data_url = "{}/services/data/v41.0/{}"
        self.pk_chunking = False

        # validate start_date
        singer_utils.strptime(default_start_date)

    def _get_standard_headers(self):
        return {"Authorization": "Bearer {}".format(self.access_token)}

    # pylint: disable=anomalous-backslash-in-string,line-too-long
    def check_rest_quota_usage(self, headers):
        match = re.search('^api-usage=(\d+)/(\d+)$', headers.get('Sforce-Limit-Info'))

        if match is None:
            return

        remaining, allotted = map(int, match.groups())

        LOGGER.info("Used %s of %s daily REST API quota", remaining, allotted)

        percent_used_from_total = (remaining / allotted) * 100
        max_requests_for_run = int((self.quota_percent_per_run * allotted) / 100)

        if percent_used_from_total > self.quota_percent_total:
            total_message = ("Quickbooks has reported {}/{} ({:3.2f}%) total REST quota " +
                             "used across all Quickbooks Applications. Terminating " +
                             "replication to not continue past configured percentage " +
                             "of {}% total quota.").format(remaining,
                                                           allotted,
                                                           percent_used_from_total,
                                                           self.quota_percent_total)
            raise TapQuickbooksQuotaExceededException(total_message)
        elif self.rest_requests_attempted > max_requests_for_run:
            partial_message = ("This replication job has made {} REST requests ({:3.2f}% of " +
                               "total quota). Terminating replication due to allotted " +
                               "quota of {}% per replication.").format(self.rest_requests_attempted,
                                                                       (self.rest_requests_attempted / allotted) * 100,
                                                                       self.quota_percent_per_run)
            raise TapQuickbooksQuotaExceededException(partial_message)

    # pylint: disable=too-many-arguments
    @backoff.on_exception(backoff.expo,
                          requests.exceptions.ConnectionError,
                          max_tries=10,
                          factor=2,
                          on_backoff=log_backoff_attempt)
    def _make_request(self, http_method, url, headers=None, body=None, stream=False, params=None):
        if http_method == "GET":
            LOGGER.info("Making %s request to %s with params: %s", http_method, url, params)
            resp = self.session.get(url, headers=headers, stream=stream, params=params)
        elif http_method == "POST":
            LOGGER.info("Making %s request to %s with body %s", http_method, url, body)
            resp = self.session.post(url, headers=headers, data=body)
        else:
            raise TapQuickbooksException("Unsupported HTTP method")

        try:
            resp.raise_for_status()
        except RequestException as ex:
            raise ex

        if resp.headers.get('Sforce-Limit-Info') is not None:
            self.rest_requests_attempted += 1
            self.check_rest_quota_usage(resp.headers)

        return resp

    def login(self):
        if self.is_sandbox:
            login_url = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
        else:
            login_url = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'

        auth_state = read_auth_state(self.gcs_auth_state_uri)

        login_body = {
            'grant_type': 'refresh_token',
            'client_id': self.qb_client_id,
            'client_secret': self.qb_client_secret,
            'refresh_token': auth_state['refresh_token'] if auth_state.get('refresh_token') else self.refresh_token
        }

        LOGGER.info("Attempting login via OAuth2")

        resp = None
        try:
            resp = self._make_request("POST", login_url, body=login_body,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})

            LOGGER.info("OAuth2 login successful")

            auth = resp.json()
            
            LOGGER.info(f"Response from Quickbooks: {json.dumps(auth)}")

            self.access_token = auth['access_token']
            self.refresh_token = auth['refresh_token']

            auth_state['access_token'] = auth['access_token']
            auth_state['refresh_token'] = auth['refresh_token']

            # persist access_token and refresh token
            write_auth_state(auth_state, self.gcs_auth_state_uri)

        except Exception as e:
            error_message = str(e)
            if resp is None and hasattr(e, 'response') and e.response is not None:  # pylint:disable=no-member
                resp = e.response  # pylint:disable=no-member
            # NB: requests.models.Response is always falsy here. It is false if status code >= 400
            if isinstance(resp, requests.models.Response):
                error_message = error_message + ", Response from Quickbooks: {}".format(resp.text)
            raise Exception(error_message) from e
        finally:
            LOGGER.info("Starting new login timer")
            self.login_timer = threading.Timer(REFRESH_TOKEN_EXPIRATION_PERIOD, self.login)
            self.login_timer.start()

    def describe(self, sobject=None):
        """Describes all objects or a specific object"""
        if sobject is None:
            return QB_OBJECTS
        else:
            return QB_OBJECT_DEFINITIONS[sobject]

    # pylint: disable=no-self-use
    def _get_selected_properties(self, catalog_entry):
        mdata = metadata.to_map(catalog_entry['metadata'])
        properties = catalog_entry['schema'].get('properties', {})

        return [k for k in properties.keys()
                if singer.should_sync_field(metadata.get(mdata, ('properties', k), 'inclusion'),
                                            metadata.get(mdata, ('properties', k), 'selected'),
                                            self.select_fields_by_default)]

    def get_start_date(self, state, catalog_entry):
        catalog_metadata = metadata.to_map(catalog_entry['metadata'])
        replication_key = catalog_metadata.get((), {}).get('replication-key')

        return (singer.get_bookmark(state,
                                    catalog_entry['tap_stream_id'],
                                    replication_key) or self.default_start_date)

    def _build_query_string(self, catalog_entry, start_date, end_date=None, order_by_clause=True):
        selected_properties = self._get_selected_properties(catalog_entry)

        query = "SELECT {} FROM {}".format("*", catalog_entry['stream'])

        catalog_metadata = metadata.to_map(catalog_entry['metadata'])
        replication_key = catalog_metadata.get((), {}).get('replication-key')

        if replication_key:
            where_clause = " WHERE {} >  '{}' ".format(
                replication_key,
                start_date)
            if end_date:
                end_date_clause = " AND {} <= {}".format(replication_key, end_date)
            else:
                end_date_clause = ""

            # order_by = " ORDERBY {} ASC".format(replication_key)
            # if order_by_clause:
            #   return query + where_clause + end_date_clause + order_by

            return query + where_clause + end_date_clause
        else:
            return query

    def query(self, catalog_entry, state, state_passed):
        if self.api_type == REST_API_TYPE:
            rest = Rest(self)
            return rest.query(catalog_entry, state)
        else:
            raise TapQuickbooksException(
                "api_type should be REST was: {}".format(
                    self.api_type))

    def query_report(self, catalog_entry, state, state_passed):
        start_date = singer_utils.strptime_with_tz(self.get_start_date(state, catalog_entry))
        if catalog_entry["stream"] == "BalanceSheetReport":
            reader = BalanceSheetReport(self, start_date, state_passed)
        elif catalog_entry["stream"] == "MonthlyBalanceSheetReport":
            reader = MonthlyBalanceSheetReport(self, start_date, state_passed)
        elif catalog_entry["stream"] == "GeneralLedgerAccrualReport":
            reader = GeneralLedgerAccrualReport(self, start_date, state_passed)
        elif catalog_entry["stream"] == "GeneralLedgerCashReport":
            reader = GeneralLedgerCashReport(self, start_date, state_passed)
        elif catalog_entry["stream"] == "CashFlowReport":
            reader = CashFlowReport(self, start_date, state_passed)
        elif catalog_entry["stream"] == "DailyCashFlowReport":
            reader = DailyCashFlowReport(self, start_date, state_passed)
        elif catalog_entry["stream"] == "MonthlyCashFlowReport":
            reader = MonthlyCashFlowReport(self, start_date, state_passed)
        elif catalog_entry["stream"] == "TransactionListReport":
            reader = TransactionListReport(self, start_date, state_passed)
        else:
            reader = ProfitAndLossDetailReport(self, start_date, state_passed)
        return reader.sync(catalog_entry)
