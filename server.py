from fastmcp import FastMCP, Context
from typing import Any, Dict, List, Optional
from datetime import datetime
import os
import logging
import requests

# Load environment variables FIRST
from dotenv import load_dotenv
load_dotenv()

# Import OAuth modules after environment is loaded
from oauth.google_auth import format_customer_id, get_headers_with_auto_token, execute_gaql, API_VERSION

# Get environment variables
GOOGLE_ADS_DEVELOPER_TOKEN = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('google_ads_server')

mcp = FastMCP("Google Ads Tools")

# Server startup
logger.info("Starting Google Ads MCP Server...")

def get_customer_name(customer_id: str) -> str:
    """Retrieve descriptive_name for the given customer ID."""
    try:
        query = "SELECT customer.descriptive_name FROM customer"
        result = execute_gaql(customer_id, query)
        rows = result.get('results', [])
        if not rows:
            return "Name not available (no results)"
        customer = rows[0].get('customer', {})
        return customer.get('descriptiveName', "Name not available (missing field)")
    except Exception:
        return "Name not available (error)"

def is_manager_account(customer_id: str) -> bool:
    """Check if a customer account is a manager (MCC)."""
    try:
        query = "SELECT customer.manager FROM customer"
        result = execute_gaql(customer_id, query)
        rows = result.get('results', [])
        if not rows:
            return False
        return bool(rows[0].get('customer', {}).get('manager', False))
    except Exception:
        return False

def get_sub_accounts(manager_id: str) -> List[Dict[str, Any]]:
    """List sub-accounts under a manager account."""
    try:
        query = (
            "SELECT customer_client.id, customer_client.descriptive_name, "
            "customer_client.level, customer_client.manager "
            "FROM customer_client WHERE customer_client.level > 0"
        )
        result = execute_gaql(manager_id, query)
        rows = result.get('results', [])
        subs = []
        for row in rows:
            client = row.get('customerClient', {}) or row.get('customer_client', {})
            cid = format_customer_id(str(client.get('id', '')))
            subs.append({
                'id': cid,
                'name': client.get('descriptiveName', f"Sub-account {cid}"),
                'access_type': 'managed',
                'is_manager': bool(client.get('manager', False)),
                'parent_id': manager_id,
                'level': int(client.get('level', 0))
            })
        return subs
    except Exception:
        return []

# ---------------------------------------------------------------------------
# Name-to-ID resolution (caches load once per server session)
# ---------------------------------------------------------------------------

_account_cache: Dict[str, str] | None = None  # lowercase name -> formatted customer ID
_manager_map: Dict[str, str] | None = None    # customer_id -> parent manager ID


def _load_account_cache() -> tuple[Dict[str, str], Dict[str, str]]:
    """Populate account name and manager caches from list_accounts logic."""
    global _account_cache, _manager_map
    if _account_cache is not None and _manager_map is not None:
        return _account_cache, _manager_map

    _account_cache = {}
    _manager_map = {}

    headers = get_headers_with_auto_token()
    url = f"https://googleads.googleapis.com/{API_VERSION}/customers:listAccessibleCustomers"
    resp = requests.get(url, headers=headers)
    if not resp.ok:
        return _account_cache, _manager_map

    resource_names = resp.json().get('resourceNames', [])
    seen = set()

    for resource in resource_names:
        cid = resource.split('/')[-1]
        fid = format_customer_id(cid)
        name = get_customer_name(fid)
        manager = is_manager_account(fid)

        if name and not name.startswith("Name not available"):
            _account_cache[name.lower()] = fid
        seen.add(fid)

        if manager:
            subs = get_sub_accounts(fid)
            for sub in subs:
                if sub['id'] not in seen:
                    sub_name = sub.get('name', '')
                    if sub_name and not sub_name.startswith("Sub-account"):
                        _account_cache[sub_name.lower()] = sub['id']
                    _manager_map[sub['id']] = fid
                    seen.add(sub['id'])
                    if sub['is_manager']:
                        nested = get_sub_accounts(sub['id'])
                        for n in nested:
                            if n['id'] not in seen:
                                n_name = n.get('name', '')
                                if n_name and not n_name.startswith("Sub-account"):
                                    _account_cache[n_name.lower()] = n['id']
                                _manager_map[n['id']] = sub['id']
                                seen.add(n['id'])

    return _account_cache, _manager_map


def _resolve_account(value: str) -> str:
    """Resolve an account name or ID to a formatted customer ID.

    - Numeric values (with or without dashes) are returned as-is after formatting.
    - Non-numeric values are looked up by name: exact match first, then substring.
    - If multiple substring matches resolve to different IDs, raises ValueError.
    """
    stripped = value.replace('-', '').replace(' ', '')
    if stripped.isdigit():
        return format_customer_id(stripped)

    cache, _ = _load_account_cache()
    key = value.lower().strip()

    # Exact match
    if key in cache:
        return cache[key]

    # Substring/partial match
    matches = [(k, v) for k, v in cache.items() if key in k]
    unique_ids = set(v for _, v in matches)
    if len(unique_ids) == 1:
        return unique_ids.pop()
    if len(unique_ids) > 1:
        raise ValueError(f"Ambiguous account '{value}' - matches: {[m[0] for m in matches]}")
    raise ValueError(f"Account '{value}' not found. Use list_accounts to see available accounts.")


def _auto_manager_id(customer_id: str, manager_id: str) -> str:
    """Auto-fill manager_id from cache if the account is managed and no manager_id was provided."""
    if manager_id:
        return manager_id
    _, mgr_map = _load_account_cache()
    return mgr_map.get(customer_id, "")


@mcp.tool
def run_gaql(
    customer_id: str,
    query: str,
    manager_id: str = "",
    ctx: Context = None
) -> Dict[str, Any]:
    """Execute a Google Ads Query Language (GAQL) query against a customer account.

    Args:
        customer_id: Customer ID (10 digits) or account name. Names are resolved via cached account list.
        query: GAQL query string. See gaql://reference resource for syntax and examples.
        manager_id: MCC manager ID or name. Auto-filled for managed sub-accounts if omitted. Only needed to override the auto-detected manager.
    """
    if ctx:
        ctx.info(f"Executing GAQL query for customer {customer_id}...")
        ctx.info(f"Query: {query}")

    if not GOOGLE_ADS_DEVELOPER_TOKEN:
        raise ValueError("Google Ads Developer Token is not set in environment variables.")

    try:
        customer_id = _resolve_account(customer_id)
        if manager_id:
            manager_id = _resolve_account(manager_id)
        manager_id = _auto_manager_id(customer_id, manager_id)
        result = execute_gaql(customer_id, query, manager_id)
        if ctx:
            ctx.info(f"GAQL query successful. Found {result['totalRows']} rows.")
        return result
    except Exception as e:
        if ctx:
            ctx.error(f"GAQL query failed: {str(e)}")
        raise

@mcp.tool
def list_accounts(ctx: Context = None) -> Dict[str, Any]:
    """List all accessible Google Ads accounts including sub-accounts under MCC managers.

    Returns accounts with access_type ('direct' or 'managed') and is_manager flag.
    Use this to determine which accounts need manager_id in run_gaql/run_keyword_planner:
    - access_type='direct': query directly, no manager_id needed
    - access_type='managed': must pass parent_id as manager_id in other tool calls
    """
    if ctx:
        ctx.info("Checking credentials and preparing to list accounts...")

    if not GOOGLE_ADS_DEVELOPER_TOKEN:
        raise ValueError("Google Ads Developer Token is not set in environment variables.")

    try:
        # This will automatically trigger OAuth flow if needed
        headers = get_headers_with_auto_token()
        
        # Fetch top-level accessible customers
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers:listAccessibleCustomers"
        resp = requests.get(url, headers=headers)
        if not resp.ok:
            if ctx:
                ctx.error(f"Failed to list accessible accounts: {resp.status_code} {resp.reason}")
            raise Exception(
                f"Error listing accounts: {resp.status_code} {resp.reason} - {resp.text}"
            )
        data = resp.json()
        resource_names = data.get('resourceNames', [])
        if not resource_names:
            if ctx:
                ctx.info("No accessible Google Ads accounts found.")
            return {'accounts': [], 'message': 'No accessible accounts found.'}

        if ctx:
            ctx.info(f"Found {len(resource_names)} top-level accessible accounts. Fetching details...")

        accounts = []
        seen = set()
        for resource in resource_names:
            cid = resource.split('/')[-1]
            fid = format_customer_id(cid)
            name = get_customer_name(fid)
            manager = is_manager_account(fid)
            account = {
                'id': fid,
                'name': name,
                'access_type': 'direct',
                'is_manager': manager,
                'level': 0
            }
            accounts.append(account)
            seen.add(fid)
            # Include sub-accounts (and nested)
            if manager:
                subs = get_sub_accounts(fid)
                for sub in subs:
                    if sub['id'] not in seen:
                        accounts.append(sub)
                        seen.add(sub['id'])
                        # nested level
                        if sub['is_manager']:
                            nested = get_sub_accounts(sub['id'])
                            for n in nested:
                                if n['id'] not in seen:
                                    accounts.append(n)
                                    seen.add(n['id'])

        if ctx:
            ctx.info(f"Finished processing. Found a total of {len(accounts)} accounts.")

        return {
            'accounts': accounts,
            'total_accounts': len(accounts)
        }
    except Exception as e:
        if ctx:
            ctx.error(f"Error listing accounts: {str(e)}")
        raise

@mcp.tool
def run_keyword_planner(
    customer_id: str,
    keywords: List[str],
    manager_id: str = "",
    page_url: Optional[str] = None,
    start_year: Optional[int] = None,
    start_month: Optional[str] = None,
    end_year: Optional[int] = None,
    end_month: Optional[str] = None,
    language_id: Optional[int] = None,
    geo_target_constants: Optional[List[int]] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """Generate keyword ideas from seed keywords or a page URL using Google Ads KeywordPlanIdeaService.

    Args:
        customer_id: Customer ID (10 digits) or account name. Names are resolved via cached account list.
        keywords: Seed keywords to generate ideas from. At least one of keywords or page_url required.
        manager_id: MCC manager ID or name. Auto-filled for managed sub-accounts if omitted. Only needed to override the auto-detected manager.
        page_url: URL related to your business to seed keyword ideas from
        start_year: Start year for historical data (default: previous year)
        start_month: Start month for historical data (default: JANUARY). Values: JANUARY-DECEMBER
        end_year: End year for historical data (default: current year)
        end_month: End month for historical data (default: current month). Values: JANUARY-DECEMBER
        language_id: Language constant ID for filtering keyword data (default: 1000=English). Common: 1000=English, 1001=German, 1002=French, 1004=Italian
        geo_target_constants: List of geo target constant IDs for country/region filtering (default: [2840]=USA). Common: 2756=Switzerland, 2250=France, 2276=Germany, 2826=UK. Pass multiple for combined targeting e.g. [2756, 2250]
    """
    if ctx:
        ctx.info(f"Generating keyword ideas for customer {customer_id}...")

    if not GOOGLE_ADS_DEVELOPER_TOKEN:
        raise ValueError("Google Ads Developer Token is not set in environment variables.")

    if not keywords and not page_url:
        raise ValueError("At least one of keywords or page_url is required.")

    try:
        customer_id = _resolve_account(customer_id)
        if manager_id:
            manager_id = _resolve_account(manager_id)
        manager_id = _auto_manager_id(customer_id, manager_id)

        headers = get_headers_with_auto_token()
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{customer_id}:generateKeywordIdeas"

        if manager_id:
            headers['login-customer-id'] = format_customer_id(manager_id)

        # Date range defaults
        now = datetime.now()
        valid_months = ['JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
                        'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER']

        start_year_final = start_year or (now.year - 1)
        start_month_final = start_month.upper() if start_month and start_month.upper() in valid_months else 'JANUARY'
        end_year_final = end_year or now.year
        end_month_final = end_month.upper() if end_month and end_month.upper() in valid_months else now.strftime('%B').upper()

        request_body = {
            'language': f'languageConstants/{language_id}' if language_id else 'languageConstants/1000',
            'geoTargetConstants': [f'geoTargetConstants/{geo_id}' for geo_id in geo_target_constants] if geo_target_constants else ['geoTargetConstants/2840'],
            'keywordPlanNetwork': 'GOOGLE_SEARCH_AND_PARTNERS',
            'includeAdultKeywords': False,
            'pageSize': 25,
            'historicalMetricsOptions': {
                'yearMonthRange': {
                    'start': {'year': start_year_final, 'month': start_month_final},
                    'end': {'year': end_year_final, 'month': end_month_final}
                }
            }
        }

        # Set the appropriate seed based on what's provided
        if not keywords and page_url:
            request_body['urlSeed'] = {'url': page_url}
        elif keywords and not page_url:
            request_body['keywordSeed'] = {'keywords': keywords}
        elif keywords and page_url:
            request_body['keywordAndUrlSeed'] = {'url': page_url, 'keywords': keywords}

        response = requests.post(url, headers=headers, json=request_body)

        if not response.ok:
            error_text = response.text
            if ctx:
                ctx.error(f"Keyword planner request failed: {response.status_code} {response.reason}")
            raise Exception(f"Error executing request: {response.status_code} {response.reason} - {error_text}")

        results = response.json()

        if 'results' not in results or not results['results']:
            return {"message": "No keyword ideas found.", "count": 0}

        # Format results with token-efficient keys and pre-converted bid values
        formatted_results = []
        for result in results['results']:
            m = result.get('keywordIdeaMetrics', {})
            low_bid = m.get('lowTopOfPageBidMicros')
            high_bid = m.get('highTopOfPageBidMicros')
            formatted_results.append({
                'keyword': result.get('text', 'N/A'),
                'volume': m.get('avgMonthlySearches', 'N/A'),
                'competition': m.get('competition', 'N/A'),
                'comp_index': m.get('competitionIndex', 'N/A'),
                'low_bid': round(int(low_bid) / 1_000_000, 2) if low_bid else 'N/A',
                'high_bid': round(int(high_bid) / 1_000_000, 2) if high_bid else 'N/A',
            })

        if ctx:
            ctx.info(f"Found {len(formatted_results)} keyword ideas.")

        return {
            "count": len(formatted_results),
            "keywords": formatted_results
        }

    except Exception as e:
        if ctx:
            ctx.error(f"An unexpected error occurred: {e}")
        raise

@mcp.resource("gaql://reference")
def gaql_reference() -> str:
    """Google Ads Query Language (GAQL) reference documentation."""
    return """\
## Basic Query Structure
SELECT field1, field2, ...
FROM resource_type
WHERE condition
ORDER BY field [ASC|DESC]
LIMIT n

## Common Fields

Resource: campaign.id, campaign.name, campaign.status
Ad Group: ad_group.id, ad_group.name, ad_group.status
Ads: ad_group_ad.ad.id, ad_group_ad.ad.final_urls
Keywords: ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type (use with keyword_view)

Metrics: metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions,
  metrics.conversions_value (direct revenue), metrics.all_conversions_value (includes view-through),
  metrics.ctr, metrics.average_cpc

Segments: segments.date, segments.device, segments.day_of_week

## WHERE Clauses

Date: segments.date DURING LAST_7_DAYS | LAST_30_DAYS | BETWEEN '2023-01-01' AND '2023-01-31'
Filter: campaign.status = 'ENABLED' | metrics.clicks > 100 | campaign.name LIKE '%Brand%'
Note: Use LIKE not CONTAINS (CONTAINS is not supported). Date ranges must be finite.

## Examples

1. Campaign metrics:
SELECT campaign.id, campaign.name, metrics.clicks, metrics.impressions, metrics.cost_micros
FROM campaign WHERE segments.date DURING LAST_7_DAYS

2. Ad group performance:
SELECT campaign.id, ad_group.name, metrics.conversions, metrics.cost_micros, campaign.name
FROM ad_group WHERE metrics.clicks > 100

3. Keyword analysis:
SELECT campaign.id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, metrics.ctr
FROM keyword_view WHERE segments.date DURING LAST_30_DAYS ORDER BY metrics.impressions DESC

4. Conversion data with revenue:
SELECT campaign.id, campaign.name, metrics.conversions, metrics.conversions_value, metrics.cost_micros
FROM campaign WHERE segments.date DURING LAST_30_DAYS

## Common Errors
- WRONG: campaign.campaign_budget.amount_micros -> CORRECT: campaign_budget.amount_micros (separate resource)
- WRONG: keyword.text -> CORRECT: ad_group_criterion.keyword.text
- Always include campaign.id when querying ad_group, keyword_view, or related resources"""

if __name__ == "__main__":
    import sys
    
    # Check command line arguments for transport mode
    if "--http" in sys.argv:
        logger.info("Starting with HTTP transport on http://127.0.0.1:8000/mcp")
        mcp.run(transport="streamable-http", host="127.0.0.1", port=8000, path="/mcp")
    else:
        # Default to STDIO for Claude Desktop compatibility
        logger.info("Starting with STDIO transport for Claude Desktop")
        mcp.run(transport="stdio")