import json
import click
import datetime
import re

from two1.commands.status import _get_balances
from two1.commands.config import TWO1_MERCHANT_HOST
from two1.commands.config import TWO1_HOST
from two1.lib.server import rest_client
from two1.commands.formatters import search_formatter
from two1.commands.formatters import sms_formatter
from two1.lib.server.analytics import capture_usage
from two1.lib.bitrequests import OnChainRequests
from two1.lib.bitrequests import BitTransferRequests
from two1.lib.bitrequests import ChannelRequests
from two1.lib.bitrequests import ResourcePriceGreaterThanMaxPriceError
from two1.lib.util.uxstring import UxString
from two1.lib.wallet.fees import get_fees
from two1.lib.channels.statemachine import PaymentChannelStateMachine


URL_REGEXP = re.compile(
    r'^(?:http)s?://'  # http:// or https://
    # domain...
    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
    r'localhost|'  # localhost...
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
    r'(?::\d+)?'  # optional port
    r'(?:/?|[/?]\S+)$', re.IGNORECASE)

DEMOS = {
    "search": {"path": "/search/bing", "formatter": search_formatter},
    "sms": {"path": "/phone/send-sms", "formatter": sms_formatter}
}

@click.group()
@click.option('-p', '--payment-method', default='offchain', type=click.Choice(['offchain', 'onchain', 'channel']))
@click.option('--maxprice', default=10000, help="Maximum amount to pay")
@click.option('-i', '--info', 'info_only', default=False, is_flag=True, help="Retrieve initial 402 payment information.")
@click.pass_context
def buy(ctx, payment_method, maxprice, info_only):
    """Buy API calls with mined bitcoin.

\b
Usage
-----
Execute a search query for bitcoin. See no ads.
$ 21 buy search "Satoshi Nakamoto"

\b
See the price in Satoshis of one bitcoin-payable search.
$ 21 buy --info search

\b
See the help for search.
$ 21 buy search -h

\b
Send an SMS to a phone number.
$ 21 buy sms +15005550002 "I just paid for this SMS with BTC"

\b
See the price in Satoshis of one bitcoin-payable sms.
$ 21 buy --info sms

\b
See the help for sms.
$ 21 buy sms -h
"""
    ctx.obj["payment_method"] = payment_method
    ctx.obj["maxprice"] = maxprice
    ctx.obj["info_only"] = info_only

    # Bypass subcommand if the user is only requesting its 402 information
    if ctx.invoked_subcommand and ctx.invoked_subcommand != "url" and info_only:
        _buy(ctx.obj["config"], ctx.invoked_subcommand,
             None, None, None, None,
             payment_method, maxprice, info_only)
        ctx.exit()


@click.argument('query')
@buy.command()
@click.pass_context
def search(ctx, query):
    """Execute a search query for bitcoin. See no ads.

\b
Example
-------
$ 21 buy search "First Bitcoin Computer"
"""
    _buy(ctx.obj["config"],
         "search",
         dict(query=query),
         "GET",
         None,
         None,
         ctx.obj["payment_method"],
         ctx.obj["maxprice"],
         ctx.obj["info_only"]
         )


@click.argument('body')
@click.argument('phone_number')
@buy.command()
@click.pass_context
def sms(ctx, phone_number, body):
    """Send an SMS to a phone number.

\b
Example
-------
$ 21 buy sms +15005550002 "I just paid for this SMS with BTC"
"""
    _buy(ctx.obj["config"],
         "sms",
         dict(phone=phone_number, text=body),
         "POST",
         None,
         None,
         ctx.obj["payment_method"],
         ctx.obj["maxprice"],
         ctx.obj["info_only"]
         )


@click.argument('resource', nargs=1)
@click.option('-X', '--request', 'method', default='GET', help="HTTP request method")
@click.option('-d', '--data', default=None, help="Data to send in HTTP body")
@click.option('--data-file', type=click.File('rb'), help="Data file to send in HTTP body")
@click.option('-o', '--output', 'output_file', type=click.File('wb'), help="Output file")
@buy.command()
@click.pass_context
def url(ctx, resource, data, method, data_file, output_file):
    """Buy any machine payable endpoint.

\b
Example
-------
$ 21 buy url https://market.21.co/phone/send-sms --data '{"phone":"+15005550002","text":"hi"}'
"""
    _buy(ctx.obj["config"],
         resource,
         data,
         method,
         data_file,
         output_file,
         ctx.obj["payment_method"],
         ctx.obj["maxprice"],
         ctx.obj["info_only"]
         )


@capture_usage
def _buy(config, resource, data, method, data_file, output_file,
         payment_method, max_price, info_only):
    # If resource is a URL string, then bypass seller search
    if URL_REGEXP.match(resource):
        target_url = resource
        seller = target_url
    elif resource in DEMOS:
        target_url = TWO1_MERCHANT_HOST + DEMOS[resource]["path"]
        data = json.dumps(data)
    else:
        raise NotImplementedError('Endpoint search is not implemented!')

    # Change default HTTP method from "GET" to "POST", if we have data
    if method == "GET" and (data or data_file):
        method = "POST"

    # Set default headers for making bitrequests with JSON-like data
    headers = {'Content-Type': 'application/json'}

    try:
        # Find the correct payment method
        if payment_method == 'offchain':
            bit_req = BitTransferRequests(config.machine_auth, config.username)
        elif payment_method == 'onchain':
            bit_req = OnChainRequests(config.wallet)
        elif payment_method == 'channel':
            bit_req = ChannelRequests(config.wallet)
            channel_list = bit_req._channelclient.list()
            if not channel_list:
                confirmed = click.confirm(UxString.buy_channel_warning.format(
                    bit_req.DEFAULT_DEPOSIT_AMOUNT,
                    PaymentChannelStateMachine.PAYMENT_TX_MIN_OUTPUT_AMOUNT), default=True)
                if not confirmed:
                    raise Exception(UxString.buy_channel_aborted)

        else:
            raise Exception('Payment method does not exist.')

        # Make the request
        if info_only:
            res = bit_req.get_402_info(target_url)
        else:
            res = bit_req.request(
                method.lower(), target_url, max_price=max_price,
                data=data or data_file, headers=headers)
    except ResourcePriceGreaterThanMaxPriceError as e:
        config.log(UxString.Error.resource_price_greater_than_max_price.format(e))
        return
    except Exception as e:
        f = get_fees()
        buy_fee = 2 * f['per_input'] + f['per_output']
        if 'Insufficient funds.' in str(e):
            config.log(UxString.Error.insufficient_funds_mine_more.format(
                buy_fee
            ))
        else:
            config.log(str(e), fg="red")
        return

    # Output results to user
    if output_file:
        # Write response output file
        output_file.write(res.content)
    elif info_only:
        # Print headers that are related to 402 payment required
        for key, val in res.items():
            config.log('{}: {}'.format(key, val))
    elif resource in DEMOS:
        config.log(DEMOS[resource]["formatter"](res))
    else:
        # Write response to console
        config.log(res.text)

    # Write the amount paid out if something was truly paid
    if not info_only and hasattr(res, 'amount_paid'):
        client = rest_client.TwentyOneRestClient(TWO1_HOST,
                                                 config.machine_auth,
                                                 config.username)
        user_balances = _get_balances(config, client)
        if payment_method == 'offchain':
            balance_amount = user_balances.twentyone
            balance_type = '21.co'
        elif payment_method == 'onchain':
            balance_amount = user_balances.onchain
            balance_type = 'blockchain'
        elif payment_method == 'channel':
            balance_amount = user_balances.channels
            balance_type = 'payment channels'
        config.log("You spent: %s Satoshis. Remaining %s balance: %s Satoshis." % (
            res.amount_paid, balance_type, balance_amount))

    # Record the transaction if it was a payable request
    if hasattr(res, 'paid_amount'):
        config.log_purchase(s=seller,
                            r=resource,
                            p=res.paid_amount,
                            d=str(datetime.datetime.today()))
