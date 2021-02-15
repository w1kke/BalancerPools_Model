import typing
import argparse
import json
import os
import pickle
from dataclasses import dataclass, field
from dataclasses_json import dataclass_json
from web3 import Web3, HTTPProvider
from w3_utils import ERC20InfoReader, BPoolLogCallParser, TransactionReceiptGetter
from marshmallow import fields
from datetime import datetime
import time

import pandas as pd
from dataclasses_json import dataclass_json, config
from google.cloud import bigquery
from decimal import Decimal

parser = argparse.ArgumentParser(prog="pulldata",
                                 description="Ask Google Bigquery about a particular Balancer pool. Remember to set GOOGLE_APPLICATION_CREDENTIALS from https://cloud.google.com/docs/authentication/getting-started")
parser.add_argument("pool_address")
args = parser.parse_args()
w3 = Web3(Web3.HTTPProvider(os.environ['NODE_URL']))
erc20_info_getter = ERC20InfoReader(w3)
log_call_parser = BPoolLogCallParser(erc20_info_getter)
receipt_getter = TransactionReceiptGetter(w3, args.pool_address)
ZERO_ADDRESS = '0x0000000000000000000000000000000000000000'


@dataclass_json
@dataclass
class Action:
    timestamp: datetime = field(
        metadata=config(
            encoder=datetime.isoformat,
            decoder=datetime.fromisoformat,
            mm_field=fields.DateTime(format="iso")
        )
    )
    tx_hash: str
    block_number: str
    swap_fee: str
    denorms: dict
    action_type: str
    action: dict


def query(client, sql: str) -> pd.DataFrame:
    print("Querying", sql)
    result = (
        client.query(sql)
            .result()
            .to_dataframe()
    )
    return result

def save_queries_pickle(pool_address: str, event_type: str, df: pd.DataFrame):
    filename = "{}/{}.pickle".format(pool_address, event_type)
    print("Pickling to", filename)
    if not os.path.exists(args.pool_address):
        os.mkdir(pool_address)
    with open(filename, 'wb') as f:
        pickle.dump(df, f)

def load_pickles(pool_address: str, event_type: str) -> pd.DataFrame:
    filename = "{}/{}.pickle".format(pool_address, event_type)
    print("Unpickling from", filename)
    with open(filename, 'rb') as f:
        return pickle.load(f)

def load_txhash_contractcalls(pool_address: str) -> typing.Dict:
    filename = "{}/txhash_contractcalls.json".format(pool_address)
    print("Using backed up tx receipts from", filename)
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_txhash_contractcalls(pool_address: str, tx_receipts: typing.Dict):
    filename = "{}/txhash_contractcalls.json".format(pool_address)
    print("Saving tx receipts to", filename)
    if not os.path.exists(args.pool_address):
        os.mkdir(pool_address)
    with open(filename, 'w') as f:
        return json.dump(tx_receipts, f)

def query_and_save(client, pool_address: str, event_type: str, sql: str, writer) -> pd.DataFrame:
    df = query(client, sql)
    writer(pool_address, event_type, df)
    return df

def get_initial_token_distribution(new_results) -> dict:
    receipt = w3.eth.getTransactionReceipt(new_results.iloc[0]['transaction_hash'])
    events = log_call_parser.parse_from_receipt(receipt, args.pool_address)
    bind_events = list(filter(lambda x: x['type'] == 'bind', events))
    tokens = {}
    total_denorm_weight = Decimal('0.0')
    for event in bind_events:
        inputs = event['inputs']
        token_address = inputs['token']
        token_symbol = erc20_info_getter.get_token_symbol(token_address)
        denorm = Decimal(inputs['denorm'])
        total_denorm_weight += denorm

        tokens[token_symbol] = {
            'weight': None,
            'denorm_weight': str(denorm),
            'balance': inputs['balance'],
            'bound': True
        }
    for (key, token) in tokens.items():
        denorm = Decimal(token['denorm_weight'])
        token['weight'] = str(denorm / total_denorm_weight)
    return tokens

def get_initial_pool_share(transfer_results, tx_hash):
    initial_tx_transfers = transfer_results.loc[transfer_results['transaction_hash'] == tx_hash]
    minting = initial_tx_transfers.loc[initial_tx_transfers['src'] == '0x0000000000000000000000000000000000000000']
    wei_amount = int(minting.iloc[0]['amt'])
    return Web3.fromWei(wei_amount, 'ether')

def format_denorms(denorms: dict) -> typing.List[typing.Dict]:
    """
    format_denorms expects the input to be
    [{'token_address': '0x89045d0af6a12782ec6f701ee6698beaf17d0ea2', 'denorm': Decimal('1000000000000000000.000000000')}, {'token_address': '0xe3b446b242ce55610ad381a8e8164c680a70f131', 'denorm': Decimal('1000000000000000000.000000000')}...]
    """
    d = []
    for item in denorms:
        a = {
            "token_address": Web3.toChecksumAddress(item['token_address']),
            "token_symbol": erc20_info_getter.get_token_symbol(item['token_address']),
            "denorm": str(Web3.fromWei(item['denorm'], 'ether')),
        }
        d.append(a)
    return d

def classify_pool_share_transfers(transfers: []) -> (str, str):
    pool_share_burnt = list(filter(lambda x: x['dst'] == ZERO_ADDRESS, transfers))
    if len(pool_share_burnt) > 0:
        return 'pool_amount_in', str(Web3.fromWei(int(pool_share_burnt[0]['amt']), 'ether'))
    pool_share_minted = list(filter(lambda x: x['src'] == ZERO_ADDRESS, transfers))
    if len(pool_share_minted) > 0:
        return 'pool_amount_out', str(Web3.fromWei(int(pool_share_minted[0]['amt']), 'ether'))
    raise Exception('not pool share mint or burn', transfers)

def map_token_amounts(txs: [], address_key: str, amount_key: str):
    def map_tx(x):
        mapped = {}
        symbol = erc20_info_getter.get_token_symbol(x[address_key])
        mapped['amount'] = erc20_info_getter.normalize_token_units(x[address_key], x[amount_key])
        mapped['symbol'] = symbol
        return mapped

    return list(map(map_tx, txs))

def classify_actions(group):
    action = {}
    transfers = list(filter(lambda x: x['action_type'] == 'transfer', group))
    if len(transfers) > 0:
        key, value = classify_pool_share_transfers(transfers[0]['action'])
        action[key] = value
    joins = list(filter(lambda x: x['action_type'] == 'join', group))
    if len(joins) > 0:
        action['type'] = 'join'
        value = map_token_amounts(joins[0]['action'], address_key='tokenIn', amount_key='tokenAmountIn')
        if len(value) == 1:
            action['token_in'] = value[0]
        else:
            action['tokens_in'] = value
    exits = list(filter(lambda x: x['action_type'] == 'exit', group))
    if len(exits) > 0:
        action['type'] = 'exit'
        value = map_token_amounts(exits[0]['action'], address_key='tokenOut', amount_key='tokenAmountOut')
        if len(value) == 1:
            action['token_out'] = value[0]
        else:
            action['tokens_out'] = value
    swaps = list(filter(lambda x: x['action_type'] == 'swap', group))
    if len(swaps) > 0:
        action['type'] = 'swap'
        in_value = map_token_amounts(swaps[0]['action'], address_key='tokenIn', amount_key='tokenAmountIn')
        if len(in_value) == 1:
            action['token_in'] = in_value[0]
        else:
            action['tokens_in'] = in_value
        out_value = map_token_amounts(swaps[0]['action'], address_key='tokenOut', amount_key='tokenAmountOut')
        if len(out_value) == 1:
            action['token_out'] = out_value[0]
        else:
            action['tokens_out'] = out_value
    if action.get('type') is None:
        action['type'] = 'pool_creation'
    if action['type'] == 'join' and action.get('token_in') is not None:
        action['type'] = 'join_swap'
    elif action['type'] == 'exit' and action.get('token_out') is not None:
        action['type'] = 'exit_swap'
    return action

def turn_events_into_actions(events_list, fees: typing.Dict, denorms: pd.DataFrame) -> typing.List[Action]:
    actions = []
    grouped = events_list.groupby("transaction_hash")
    for txhash, events in grouped:
        # Get basic info from first event log, no matter how many there actually are
        first_event_log = events.iloc[0]
        ts = first_event_log["block_timestamp"]
        tx_hash = first_event_log["transaction_hash"]
        block_number = first_event_log.name

        # Invariant data that exists parallel to these actions. Merge them
        # into the same Action object as a "context" for convenience.
        fee = fees[block_number]
        denorm = format_denorms(denorms.loc[block_number].to_dict(orient="records"))
        # convert block_number and swap_fee to string to painlessly
        # convert to JSON later (numpy.int64 can't be JSON serialized)
        a = Action(timestamp=ts.to_pydatetime(), tx_hash=tx_hash, block_number=str(block_number), swap_fee=str(fee),
                    denorms=denorm, action_type=first_event_log["type"], action=events.to_dict(orient="records"))
        actions.append(a)

    return actions

def stage1_load_sql_data(pool_address: str):
    if not os.path.exists(args.pool_address):
        new_sql = 'select * from blockchain-etl.ethereum_balancer.BFactory_event_LOG_NEW_POOL where pool="{}"'.format(args.pool_address)
        swap_sql = 'select * from blockchain-etl.ethereum_balancer.BPool_event_LOG_SWAP where contract_address="{}" order by block_number'.format(
            args.pool_address)
        join_sql = 'select * from blockchain-etl.ethereum_balancer.BPool_event_LOG_JOIN where contract_address="{}" order by block_number'.format(
            args.pool_address)
        exit_sql = 'select * from blockchain-etl.ethereum_balancer.BPool_event_LOG_EXIT where contract_address="{}" order by block_number'.format(
            args.pool_address)
        transfer_sql = 'select * from blockchain-etl.ethereum_balancer.BPool_event_Transfer where contract_address="{}" order by block_number'.format(
            args.pool_address)
        with open("view_pools_fees.sql", "r") as f:
            fees_sql = f.read().format(args.pool_address)
        with open("view_pools_tokens_denorm_weights.sql", "r") as f:
            denorms_sql = f.read().format(args.pool_address)

        client = bigquery.Client()

        new_results = query_and_save(client, args.pool_address, "new", new_sql, save_queries_pickle)
        join_results = query_and_save(client, args.pool_address, "join", join_sql, save_queries_pickle)
        swap_results = query_and_save(client, args.pool_address, "swap", swap_sql, save_queries_pickle)
        exit_results = query_and_save(client, args.pool_address, "exit", exit_sql, save_queries_pickle)
        transfer_results = query_and_save(client, args.pool_address, "transfer", transfer_sql, save_queries_pickle)
        fees_results = query_and_save(client, args.pool_address, "fees", fees_sql, save_queries_pickle)
        denorms_results = query_and_save(client, args.pool_address, "denorms", denorms_sql, save_queries_pickle)

    else:
        new_results = load_pickles(args.pool_address, "new").set_index("block_number")
        join_results = load_pickles(args.pool_address, "join").set_index("block_number")
        swap_results = load_pickles(args.pool_address, "swap").set_index("block_number")
        exit_results = load_pickles(args.pool_address, "exit").set_index("block_number")
        transfer_results = load_pickles(args.pool_address, "transfer").set_index("block_number")
        fees_results = load_pickles(args.pool_address, "fees").set_index("block_number")
        denorms_results = load_pickles(args.pool_address, "denorms").set_index("block_number")

    return new_results, join_results, swap_results, exit_results, transfer_results, fees_results, denorms_results

def stage2_produce_initial_state(new_results, fees_results, transfer_results):
    tokens = get_initial_token_distribution(new_results)
    swap_fee_weis = int(fees_results.iloc[0]['swapFee'])
    swap_fee = Web3.fromWei(swap_fee_weis, 'ether')
    pool_shares = get_initial_pool_share(transfer_results, new_results.iloc[0]['transaction_hash'])
    creation_date = new_results.iloc[0]['block_timestamp'].isoformat()
    initial_states = {
        'pool': {
            'tokens': tokens,
            'generated_fees': 0.0,
            'pool_shares': str(pool_shares),
            'swap_fee': str(swap_fee)
        },
        'action_type': 'pool_creation',
        'change_datetime': creation_date
    }
    print(initial_states)
    initial_states_filename = args.pool_address + "-initial_pool_states.json"
    with open(initial_states_filename, 'w') as f:
        json.dump(initial_states, f, indent="\t")
        print("Saved to", initial_states_filename)

def stage3_merge_actions(pool_address, grouped_actions):
    tx_receipts = load_txhash_contractcalls(pool_address)

    actions_final = []
    for group in grouped_actions:
        merged_action = {
            "timestamp": group[0]['timestamp'],
            "tx_hash": group[0]['tx_hash'],
            "block_number": group[0]['block_number'],
            "swap_fee": str(Web3.fromWei(int(group[0]['swap_fee']), 'ether')),
            "denorms": group[0]['denorms']
        }
        r = tx_receipts.get(merged_action['tx_hash'])
        if not r:
            print('merging tx', merged_action['tx_hash'])
            time.sleep(0.05)
            receipt = w3.eth.getTransactionReceipt(merged_action['tx_hash'])
            input_data = log_call_parser.parse_from_receipt(receipt, pool_address)
            merged_action['contract_call'] = input_data
            tx_receipts[merged_action["tx_hash"]] = input_data
        else:
            merged_action['contract_call'] = r

        merged_action['action'] = classify_actions(group)
        actions_final.append(merged_action)

    save_txhash_contractcalls(pool_address, tx_receipts)

    actions_final.sort(key=lambda a: a['timestamp'])
    return actions_final

def produce_actions():
        new_results, join_results, swap_results, exit_results, transfer_results, fees_results, denorms_results = stage1_load_sql_data(args.pool_address)

        new_results["type"] = "new"
        join_results["type"] = "join"
        swap_results["type"] = "swap"
        exit_results["type"] = "exit"
        transfer_results["type"] = "transfer"

        # Later we will drop the column "address" from denorms, because it is
        # just the pool address - it never changes.
        denorms_results.drop(["address"], axis=1, inplace=True)
        # Using loc is slow. To avoid costly lookups by block_number, we convert
        # swapFee lookups into a dict, and denorms too
        fees_dict = fees_results.drop("address", axis=1).to_dict()["swapFee"]

        # Pandas, please don't truncate columns when I print them out
        pd.set_option('display.max_colwidth', None)

        stage2_produce_initial_state(new_results, fees_results, transfer_results)

        actions = []
        actions.extend(turn_events_into_actions(new_results, fees_dict, denorms_results))
        actions.extend(turn_events_into_actions(join_results, fees_dict, denorms_results))
        actions.extend(turn_events_into_actions(swap_results, fees_dict, denorms_results))
        actions.extend(turn_events_into_actions(exit_results, fees_dict, denorms_results))
        actions.extend(turn_events_into_actions(transfer_results, fees_dict, denorms_results))

        grouped_by_tx_actions = {}
        for i, action in enumerate(actions):
            tx_hash = actions[i].tx_hash
            if grouped_by_tx_actions.get(tx_hash) is None:
                grouped_by_tx_actions[tx_hash] = []
            grouped_by_tx_actions[tx_hash].append(action.to_dict())
        grouped_actions = list(map(lambda key: grouped_by_tx_actions[key], grouped_by_tx_actions))

        # Filter out pool share transfer
        grouped_actions = list(filter(lambda acts: not (len(acts) == 1 and acts[0]['action_type'] == 'transfer'), grouped_actions))

        # with open("{}/grouped_actions.pickle".format(args.pool_address), "rb") as f:
        #     grouped_actions = pickle.load(f)

        actions_final = stage3_merge_actions(args.pool_address, grouped_actions)
        actions_filename = args.pool_address + "-actions.json"
        print("saving to", actions_filename)
        with open(actions_filename, 'w') as f:
            json.dump(actions_final, f, indent="\t")


from ipdb import launch_ipdb_on_exception

with launch_ipdb_on_exception():
    produce_actions()
