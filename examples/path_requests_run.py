#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
path_requests_run.py
====================

Reads a JSON request file in accordance with the Yang model
for requesting path computation and returns path results in terms
of path and feasibilty.

See: draft-ietf-teas-yang-path-computation-01.txt
"""

from sys import exit
from argparse import ArgumentParser
from pathlib import Path
from collections import namedtuple
from logging import getLogger, basicConfig, CRITICAL, DEBUG, INFO
from json import dumps, loads
from networkx import (draw_networkx_nodes, draw_networkx_edges,
                      draw_networkx_labels)
from numpy import mean
from gnpy.core.service_sheet import convert_service_sheet, Request_element, Element
from gnpy.core.utils import load_json
from gnpy.core.network import load_network, build_network, set_roadm_loss, save_network
from gnpy.core.equipment import load_equipment, trx_mode_params, automatic_nch, automatic_spacing
from gnpy.core.elements import Transceiver, Roadm, Edfa, Fused
from gnpy.core.utils import db2lin, lin2db
from gnpy.core.request import (Path_request, Result_element, compute_constrained_path,
                              propagate, jsontocsv, Disjunction, compute_path_dsjctn, requests_aggregation,
                              propagate_and_optimize_mode)
from copy import copy, deepcopy
from textwrap import dedent
from math import ceil
import time

#EQPT_LIBRARY_FILENAME = Path(__file__).parent / 'eqpt_config.json'

logger = getLogger(__name__)

parser = ArgumentParser(description = 'A function that computes performances for a list of services provided in a json file or an excel sheet.')
parser.add_argument('network_filename', nargs='?', type = Path, default= Path(__file__).parent / 'meshTopologyExampleV2.xls')
parser.add_argument('service_filename', nargs='?', type = Path, default= Path(__file__).parent / 'meshTopologyExampleV2.xls')
parser.add_argument('eqpt_filename', nargs='?', type = Path, default=Path(__file__).parent / 'eqpt_config.json')
parser.add_argument('-v', '--verbose', action='count', default=0, help='increases verbosity for each occurence')
parser.add_argument('-o', '--output')


def requests_from_json(json_data,equipment):
    requests_list = []

    for req in json_data['path-request']:
        # init all params from request
        params = {}
        params['request_id'] = req['request-id']
        params['source'] = req['src-tp-id']
        params['destination'] = req['dst-tp-id']
        params['trx_type'] = req['path-constraints']['te-bandwidth']['trx_type']
        params['trx_mode'] = req['path-constraints']['te-bandwidth']['trx_mode']
        params['format'] = params['trx_mode']
        nd_list = req['optimizations']['explicit-route-include-objects']
        params['nodes_list'] = [n['unnumbered-hop']['node-id'] for n in nd_list]
        params['loose_list'] = [n['unnumbered-hop']['hop-type'] for n in nd_list]
        params['spacing'] = req['path-constraints']['te-bandwidth']['spacing']

        # recover trx physical param (baudrate, ...) from type and mode
        # in trx_mode_params optical power is read from equipment['SI']['default'] and
        # nb_channel is computed based on min max frequency and spacing
        trx_params = trx_mode_params(equipment,params['trx_type'],params['trx_mode'],True)
        params.update(trx_params)
        print(trx_params['tx_osnr'])
        # optical power might be set differently in the request. if it is indicated then the 
        # params['power'] is updated
        if req['path-constraints']['te-bandwidth']['output-power']:
            params['power'] = req['path-constraints']['te-bandwidth']['output-power']
        # same process for nb-channel
        fmin = trx_params['frequency']['min']
        fmax = trx_params['frequency']['max']
        if req['path-constraints']['te-bandwidth']['max-nb-of-channel'] is not None :
            # check if requested nb_channels is consistant with baudrate and min-max frequencies
            if trx_params['baud_rate'] is not None:
                min_recommanded_spacing = automatic_spacing(trx_params['baud_rate'])
                # needed for printing - else argument with quote are making errors in the print
                temp = params['baud_rate']*1e-9
            else:
                min_recommanded_spacing = params['spacing']
                temp = 'undetermined baudrate'
        
            max_recommanded_nb_channels = automatic_nch(fmin,fmax,
                min_recommanded_spacing)
                
            if req['path-constraints']['te-bandwidth']['max-nb-of-channel'] <= max_recommanded_nb_channels :
                params['nb_channel'] = req['path-constraints']['te-bandwidth']['max-nb-of-channel']
            else:
                msg = dedent(f'''
                Requested channel number is not consistent with frequency range:
                {fmin*1e-12} THz, {fmax*1e-12} THz and baud rate: {temp} GHz
                min recommanded spacing is {min_recommanded_spacing}
                max recommanded nb of channels is {max_recommanded_nb_channels}
                Computation stopped.''')
                logger.critical(msg)
                exit()
                

        else :
            params['nb_channel'] = automatic_nch(fmin,fmax,params['spacing'])
        try :
            params['path_bandwidth'] = req['path-constraints']['te-bandwidth']['path_bandwidth']
        except KeyError:
            pass
        requests_list.append(Path_request(**params))
    return requests_list

def disjunctions_from_json(json_data):
    disjunctions_list = []

    for snc in json_data['synchronization']:
        params = {}
        params['disjunction_id'] = snc['synchronization-id']
        params['relaxable'] = snc['svec']['relaxable']
        params['link_diverse'] = snc['svec']['link-diverse']
        params['node_diverse'] = snc['svec']['node-diverse']
        params['disjunctions_req'] = snc['svec']['request-id-number']
        disjunctions_list.append(Disjunction(**params))
    print(disjunctions_list)
    return disjunctions_list


def load_requests(filename,eqpt_filename):
    if filename.suffix.lower() == '.xls':
        logger.info('Automatically converting requests from XLS to JSON')
        json_data = convert_service_sheet(filename,eqpt_filename)
    else:
        with open(filename, encoding='utf-8') as f:
            json_data = loads(f.read())
    return json_data

def compute_path(network, equipment, pathreqlist):

    # This function is obsolete and not relevant with respect to network building: suggest either to correct
    # or to suppress it
    
    path_res_list = []

    for pathreq in pathreqlist:
        #need to rebuid the network for each path because the total power
        #can be different and the choice of amplifiers in autodesign is power dependant
        #but the design is the same if the total power is the same
        #TODO parametrize the total spectrum power so the same design can be shared
        p_db = lin2db(pathreq.power*1e3)
        p_total_db = p_db + lin2db(pathreq.nb_channel)
        build_network(network, equipment, p_db, p_total_db)
        pathreq.nodes_list.append(pathreq.destination)
        #we assume that the destination is a strict constraint
        pathreq.loose_list.append('strict')
        print(f'Computing path from {pathreq.source} to {pathreq.destination}')
        print(f'with path constraint: {[pathreq.source]+pathreq.nodes_list}') #adding first node to be clearer on the output
        total_path = compute_constrained_path(network, pathreq)
        print(f'Computed path (roadms):{[e.uid for e in total_path  if isinstance(e, Roadm)]}\n')

        if total_path :
            total_path = propagate(total_path,pathreq,equipment, show=False)
        else:
            total_path = []
        # we record the last tranceiver object in order to have th whole
        # information about spectrum. Important Note: since transceivers
        # attached to roadms are actually logical elements to simulate
        # performance, several demands having the same destination may use
        # the same transponder for the performance simaulation. This is why
        # we use deepcopy: to ensure each propagation is recorded and not
        # overwritten

        path_res_list.append(deepcopy(total_path))
    return path_res_list

def compute_path_with_disjunction(network, equipment, pathreqlist, pathlist):
    
    # use a list but a dictionnary might be helpful to find path bathsed on request_id
    # TODO change all these req, dsjct, res lists into dict !
    path_res_list = []

    for i,pathreq in enumerate(pathreqlist):

        # use the power specified in requests but might be different from the one specified for design
        # the power is an optional parameter for requests definition
        # if optional, use the one defines in eqt_config.json
        p_db = lin2db(pathreq.power*1e3)
        p_total_db = p_db + lin2db(pathreq.nb_channel)
        print(f'request {pathreq.request_id}')
        print(f'Computing path from {pathreq.source} to {pathreq.destination}')
        print(f'with path constraint: {[pathreq.source]+pathreq.nodes_list}') #adding first node to be clearer on the output

        total_path = pathlist[i]
        print(f'Computed path (roadms):{[e.uid for e in total_path  if isinstance(e, Roadm)]}\n')
        # for debug
        # print(f'{pathreq.baud_rate}   {pathreq.power}   {pathreq.spacing}   {pathreq.nb_channel}')
        if total_path :
            if pathreq.baud_rate is not None:
                total_path = propagate(total_path,pathreq,equipment, show=False)
            else:
                total_path,mode = propagate_and_optimize_mode(total_path,pathreq,equipment, show=False)
                # if no baudrate satisfies spacing, no mode is returned and an empty path is returned
                # a warning is shown in the propagate_and_optimize_mode
                if mode is not None :
                    # propagate_and_optimize_mode function returns the mode with the highest bitrate
                    # that passes. if no mode passes, then it returns an empty path
                    pathreq.baud_rate = mode['baud_rate']
                    pathreq.tsp_mode = mode['format']
                    pathreq.format = mode['format']
                    pathreq.OSNR = mode['OSNR']
                    pathreq.tx_osnr = mode['tx_osnr']
                    pathreq.bit_rate = mode['bit_rate']
                else :
                    total_path = []
        # we record the last tranceiver object in order to have th whole 
        # information about spectrum. Important Note: since transceivers 
        # attached to roadms are actually logical elements to simulate
        # performance, several demands having the same destination may use 
        # the same transponder for the performance simaulation. This is why 
        # we use deepcopy: to ensure each propagation is recorded and not 
        # overwritten 
        
        path_res_list.append(deepcopy(total_path))
    return path_res_list

def correct_route_list(network, pathreqlist):
    # prepares the format of route list of nodes to be consistant
    # remove wrong names, remove endpoints
    # also correct source and destination
    anytype = [n.uid for n in network.nodes() if not isinstance(n, Transceiver)]
    transponders = [n.uid for n in network.nodes() if isinstance(n, Transceiver)]
    for pathreq in pathreqlist:
        for i,n_id in enumerate(pathreq.nodes_list):
            # replace possibly wrong name with a formated roadm name
            # print(n_id)
            if n_id not in anytype :
                nodes_suggestion = [uid for uid in anytype \
                    if n_id.lower() in uid.lower()]
                if pathreq.loose_list[i] == 'loose':
                    if len(nodes_suggestion)>0 :
                        new_n = nodes_suggestion[0]
                        print(f'invalid route node specified:\
                        \n\'{n_id}\', replaced with \'{new_n}\'')
                        pathreq.nodes_list[i] = new_n
                    else:
                        print(f'invalid route node specified \'{n_id}\', could not use it as constraint, skipped!')
                        pathreq.nodes_list.remove(n_id)
                        pathreq.loose_list.pop(i)
                else:
                    msg = f'could not find node : {n_id} in network topology. Strict constraint can not be applied.'
                    logger.critical(msg)
                    raise ValueError(msg)

        if pathreq.source not in transponders:
            msg = f'Request: {pathreq.request_id}: could not find transponder source : {pathreq.source}.'
            logger.critical(msg)
            print(f'{msg}\nComputation stopped.')
            exit()
            
        if pathreq.destination not in transponders:
            msg = f'Request: {pathreq.request_id}: could not find transponder destination : {pathreq.destination}.'
            logger.critical(msg)
            print(f'{msg}\nComputation stopped.')
            exit()

        # TODO remove endpoints from this list in case they were added by the user in the xls or json files
    return pathreqlist

def correct_disjn(disjn):
    local_disjn = disjn.copy()
    for el in local_disjn:
        for d in local_disjn:
            if set(el.disjunctions_req) == set(d.disjunctions_req) and\
             el.disjunction_id != d.disjunction_id:
                local_disjn.remove(d)
    return local_disjn


def path_result_json(pathresult):
    data = {
        'path': [n.json for n in pathresult]
    }
    return data


if __name__ == '__main__':
    start = time.time()
    args = parser.parse_args()
    basicConfig(level={2: DEBUG, 1: INFO, 0: CRITICAL}.get(args.verbose, DEBUG))
    logger.info(f'Computing path requests {args.service_filename} into JSON format')
    # for debug
    # print( args.eqpt_filename)
    data = load_requests(args.service_filename,args.eqpt_filename)
    equipment = load_equipment(args.eqpt_filename)
    network = load_network(args.network_filename,equipment)

    # Build the network once using the default power defined in SI in eqpt config
    # TODO power density : db2linp(ower_dbm": 0)/power_dbm": 0 * nb channels as defined by
    # spacing, f_min and f_max 
    p_db = equipment['SI']['default'].power_dbm
    
    p_total_db = p_db + lin2db(automatic_nch(equipment['SI']['default'].f_min,\
        equipment['SI']['default'].f_max, equipment['SI']['default'].spacing))
    build_network(network, equipment, p_db, p_total_db)
    save_network(args.network_filename, network)

    rqs = requests_from_json(data, equipment)

    # check that request ids are unique. Non unique ids, may 
    # mess the computation : better to stop the computation
    all_ids = [r.request_id for r in rqs]
    if len(all_ids) != len(set(all_ids)):
        for a in list(set(all_ids)):
            all_ids.remove(a)
        msg = f'Requests id {all_ids} are not unique'
        logger.critical(msg)
        exit()
    rqs = correct_route_list(network, rqs)

    # pths = compute_path(network, equipment, rqs)
    dsjn = disjunctions_from_json(data)
    # print('ohohoho')
    # print(dsjn)
    # need to warn or correct in case of wrong disjunction form
    # disjunction must not be repeated with same or different ids
    dsjn = correct_disjn(dsjn)
        
    # Aggregate demands with same exact constraints
    rqs,dsjn = requests_aggregation(rqs,dsjn)
    # TODO export novel set of aggregated demands in a json file

    print('WARNING: The following services have been requested:')
    print(rqs)
    
    pths = compute_path_dsjctn(network, equipment, rqs, dsjn)
    propagatedpths = compute_path_with_disjunction(network, equipment, rqs, pths)

    end = time.time()
    print(f'computation time {end-start}')
    
    header = ['demand','snr@bandwidth','snr@0.1nm','Receiver minOSNR', 'mode', 'Gbit/s' , 'nb of tsp pairs']
    data = []
    data.append(header)
    for i, p in enumerate(propagatedpths):
        if p:
            line = [f'{rqs[i].request_id} {rqs[i].source} to {rqs[i].destination} : ', f'{round(mean(p[-1].snr),2)}',\
                f'{round(mean(p[-1].snr+lin2db(rqs[i].baud_rate/(12.5e9))),2)}',\
                f'{rqs[i].OSNR}', f'{rqs[i].tsp_mode}' , f'{round(rqs[i].path_bandwidth * 1e-9,2)}' , f'{ceil(rqs[i].path_bandwidth / rqs[i].bit_rate) }']
        else:
            line = [f'{rqs[i].request_id} no path from {rqs[i].source} to {rqs[i].destination} ']
        data.append(line)

    col_width = max(len(word) for row in data for word in row[1:])   # padding
    firstcol_width = max(len(row[0]) for row in data )   # padding
    for row in data:
        firstcol = ''.join(row[0].ljust(firstcol_width)) 
        remainingcols = ''.join(word.ljust(col_width) for word in row[1:])
        print(f'{firstcol} {remainingcols}')


    if args.output :
        result = []
        # assumes that list of rqs and list of propgatedpths have same order
        for i,p in enumerate(propagatedpths):
            result.append(Result_element(rqs[i],p))
        temp = path_result_json(result)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(dumps(path_result_json(result), indent=2, ensure_ascii=False))
            fnamecsv = next(s for s in args.output.split('.')) + '.csv'
            with open(fnamecsv,"w", encoding='utf-8') as fcsv :
                jsontocsv(temp,equipment,fcsv)

