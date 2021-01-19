#!/usr/bin/env python
# coding: utf-8

# In[1]:


from __future__ import print_function
from itertools import combinations, permutations
import logging
import networkx as nx
import numpy as np
import scipy.stats as spst
import scipy.special as spsp
import torch
from numba import cuda
from sklearn.linear_model import LinearRegression
import time
import pandas as pd
from random import random
# from tqdm import tqdm
import re
import miceforest as mf
import sys
import argparse
import matplotlib.pyplot as plt
get_ipython().run_line_magic('matplotlib', 'inline')

_logger = logging.getLogger(__name__)


# In[2]:


# This is a function to merge several nodes into one in a Networkx graph
def merge_nodes(G, nodes, new_node): # , attr_dict=None, **attr):
    """
    Merges the selected `nodes` of the graph G into one `new_node`,
    meaning that all the edges that pointed to or from one of these
    `nodes` will point to or from the `new_node`.
    attr_dict and **attr are defined as in `G.add_node`.
    """
    H = G.copy()
    
    H.add_node(new_node) # , attr_dict) # , **attr) # Add the 'merged' node
    
    for n1,n2 in G.edges(data=False):
        # For all edges related to one of the nodes to merge,
        # make an edge going to or coming from the `new gene`.
        if n1 in nodes:
            H.add_edge(new_node,n2)#,data)
        elif n2 in nodes:
            H.add_edge(n1,new_node)# ,data)
    
    for n in nodes: # remove the merged nodes
        H.remove_node(n)
    return H


# In[3]:


def _create_complete_graph(node_ids):
    """Create a complete graph from the list of node ids.

    Args:
        node_ids: a list of node ids

    Returns:
        An undirected graph (as a networkx.Graph)
    """
    g = nx.Graph()
    g.add_nodes_from(node_ids)
    for (i, j) in combinations(node_ids, 2):
        g.add_edge(i, j)
    return g


# In[4]:


def func_z_test(corr_matrix, ijk, l, g, sep_set, sample_size):
    global cont
    # Move ijk to GPU
    
    ijk = torch.LongTensor(ijk)
    if cuda:
        ijk = ijk.to(device)
    
    if l == 0:
        H = corr_matrix[ijk[:,0:2].repeat(1,2).view(-1,2,2).transpose(1,2),
                        ijk[:,0:2].repeat(1,2).view(-1,2,2)]#.cuda(device=device)
        if cuda:
            H = H.to(device)
    else:
        M0 = corr_matrix[ijk[:,0:2].repeat(1,2).view(-1,2,2).transpose(1,2),
                         ijk[:,0:2].repeat(1,2).view(-1,2,2)]#.cuda(device=device)

        M1 = corr_matrix[ijk[:,0:2].repeat(1,l).view(-1, l, 2).transpose(1,2),
                         ijk[:,2:].repeat(1,2).view(-1, 2, l)]#.cuda(device=device)

        M2 = corr_matrix[ijk[:,2:].repeat(1,l).view(-1,l,l).transpose(1,2),
                         ijk[:,2:].repeat(1,l).view(-1,l,l)]#.cuda(device=device)
        if cuda:
            M0 = M0.to(device)
            M1 = M1.to(device)
            M2 = M2.to(device)
            
        H = M0-torch.matmul(torch.matmul(M1, torch.inverse(M2)), M1.transpose(2,1))

    rho_ijs = (H[:,0,1]/torch.sqrt(H[:,0,0] * H[:,1,1]))

    # Absolute value of r, respect cut threshold
    CUT_THR = 0.999999
    rho_ijs = torch.abs(rho_ijs)
    rho_ijs = torch.clamp(rho_ijs, min=0.0 ,max=CUT_THR)
            
    #    Note: log1p for more numerical stability, see "Aaux.R";
    # z_val = torch.abs(1/2 * torch.log((1 + rho_ijs)/(1-rho_ijs)))
    z_val = 1/2 * torch.log1p((2*rho_ijs)/(1-rho_ijs))
    tau = torch.tensor(spst.norm.ppf(1-alpha/2)/np.sqrt(sample_size - l - 3) * np.ones(shape=(ijk.shape[0],)), dtype=torch.float32)

    if cuda:
        tau = tau.to(device)
        
    if cuda:
        ii = ijk[z_val <= tau, 0].cpu().numpy()
        jj = ijk[z_val <= tau, 1].cpu().numpy()
        kk = ijk[z_val <= tau, 2:].cpu().numpy()
    else:
        ii = ijk[z_val <= tau, 0].numpy()
        jj = ijk[z_val <= tau, 1].numpy()
        kk = ijk[z_val <= tau, 2:].numpy()

    for t in range(len(ii)):
        if g.has_edge(ii[t], jj[t]):
            g.remove_edge(ii[t], jj[t])
        cont = True
        sep_set[ii[t]][jj[t]] |= set(kk[t,:])
        sep_set[jj[t]][ii[t]] |= set(kk[t,:])
        # sep_set[j[t]][i[t]] = sep_set[i[t]][j[t]]
        
    # Change ijk back to CPU and reset to empty
    # ijk = ijk.cpu().numpy()
    # ijk = np.empty_like(ijk)

    # Reset index back to 0
    # index = 0
    return g, sep_set


# In[5]:


def estimate_skeleton(corr_matrix, sample_size, alpha, init_graph, know_edge_list, **kwargs):
    global cont
    """Estimate a skeleton graph from the statistis information.

    Args:
        indep_test_func: the function name for a conditional
            independency test.
        data_matrix: data (as a numpy array).
        alpha: the significance level.
        kwargs:
            'max_reach': maximum value of l (see the code).  The
                value depends on the underlying distribution.
            'method': if 'stable' given, use stable-PC algorithm
                (see [Colombo2014]).
            'init_graph': initial structure of skeleton graph
                (as a networkx.Graph). If not specified,
                a complete graph is used.
            other parameters may be passed depending on the
                indep_test_func()s.
    Returns:
        g: a skeleton graph (as a networkx.Graph).
        sep_set: a separation set (as an 2D-array of set()).
    """

    def method_stable(kwargs):
        return ('method' in kwargs) and kwargs['method'] == "stable"


    node_ids = range(corr_matrix.shape[0])

    node_size = corr_matrix.shape[0]
    sep_set = [[set() for i in range(node_size)] for j in range(node_size)]
    

    g = init_graph

                
    l = node_size - 2
    
#     torch.set_default_tensor_type(torch.DoubleTensor)
    batch_size = 5000   # (-_-#) A Magic Number   
    
    while l >= 0:
        print(f"==================> Performing round {l} .....")
        cont = False

        ijk = np.empty(shape=(batch_size,(2 + l)), dtype = int)
        
        index = 0
        
        for (i, j) in permutations(node_ids, 2):
            ### Known edges
            if know_edge_list:
                if [i, j] in know_edge_list or [j,i] in  know_edge_list:
                    continue
            
            adj_i = list(g.neighbors(i))  # g is actually changed on-the-fly, so we need g_save to test edges
            if j not in adj_i:
                continue
            else:
                adj_i.remove(j)
            if len(adj_i) >= l:
                _logger.debug('testing %s and %s' % (i,j))
                _logger.debug('neighbors of %s are %s' % (i, str(adj_i)))
                if len(adj_i) < l:
                    continue
                for k in combinations(adj_i, l):
                    # print(f"Test edge {i, j} on {k}")
                    ijk[index, 0:2] = [i,j]    # torch.LongTensor([i, j])  # .cuda(device=device)
                    ijk[index, 2:] = k         # torch.LongTensor(k)       # .cuda(device=device)
                    index += 1
                    if index == batch_size:
                        g, sep_set = func_z_test(corr_matrix, ijk, l, g, sep_set, sample_size)
                        index = 0
                            
                        
        # ******************************
        if index != 0:
            ijk_batch = ijk[:index, :]
            g, sep_set = func_z_test(corr_matrix, ijk_batch, l, g, sep_set, sample_size)
        # ***************************************
        
        l -= 1
        
        #if cont is False:
        #    break

    return (g, sep_set)


# In[6]:


def estimate_cpdag(skel_graph, sep_set, timeInfoDict, know_edge_list):
    """Estimate a CPDAG from the skeleton graph and separation sets
    returned by the estimate_skeleton() function.

    Args:
        skel_graph: A skeleton graph (an undirected networkx.Graph).
        sep_set: An 2D-array of separation set.
            The contents look like something like below.
                sep_set[i][j] = set([k, l, m])
        tiers: A dictionary of node lists. {time order: [nodes]}

    Returns:
        An estimated DAG.
    """
    
    dag = skel_graph.to_directed()
    node_ids = skel_graph.nodes()

    
    ### Direct based on Known edges
    if know_edge_list:
        for [i, j] in know_edge_list:
            if dag.has_edge(j, i):
                dag.remove_edge(j, i)
                
    ##### Direct based on Time information
    if timeInfoDict:
        node_time_dict = dict()
        for k, v in timeInfoDict.items():
            for node in v:
                node_time_dict[node] = k

        for (i, j) in combinations(node_ids, 2):
            if i in node_time_dict and j in node_time_dict:
                if node_time_dict[i] > node_time_dict[j] and dag.has_edge(i, j): # i <---- j
                    _logger.debug('S: remove edge (%s, %s)' % (j, i))
                    dag.remove_edge(i, j)
                if node_time_dict[i] < node_time_dict[j] and dag.has_edge(j, i): # i ----> j
                    _logger.debug('S: remove edge (%s, %s)' % (i, j))
                    dag.remove_edge(j, i)
                    

                
    ####  V-structure           
    for (i, j) in combinations(node_ids, 2):
        adj_i = set(dag.successors(i))
        if j in adj_i:
            continue
        adj_j = set(dag.successors(j))
        if i in adj_j:
            continue
        if sep_set[i][j] is None:
            continue
        common_k = adj_i & adj_j
        for k in common_k:
            if k not in sep_set[i][j]:
                if dag.has_edge(k, i):
                    _logger.debug('S: remove edge (%s, %s)' % (k, i))
                    dag.remove_edge(k, i)
                if dag.has_edge(k, j):
                    _logger.debug('S: remove edge (%s, %s)' % (k, j))
                    dag.remove_edge(k, j)

    def _has_both_edges(dag, i, j):
        return dag.has_edge(i, j) and dag.has_edge(j, i)

    def _has_any_edge(dag, i, j):
        return dag.has_edge(i, j) or dag.has_edge(j, i)

    def _has_one_edge(dag, i, j):
        return ((dag.has_edge(i, j) and (not dag.has_edge(j, i))) or
                (not dag.has_edge(i, j)) and dag.has_edge(j, i))

    def _has_no_edge(dag, i, j):
        return (not dag.has_edge(i, j)) and (not dag.has_edge(j, i))

    #### For all the combination of nodes i and j, apply the following
    #### rules.
    old_dag = dag.copy()
    while True:
        for (i, j) in combinations(node_ids, 2):
            # Rule 1: Orient i-j into i->j whenever there is an arrow k->i
            # such that k and j are nonadjacent.
            #
            # Check if i-j.
            if _has_both_edges(dag, i, j):
                # Look all the predecessors of i.
                for k in dag.predecessors(i):
                    # Skip if there is an arrow i->k.
                    if dag.has_edge(i, k):
                        continue
                    # Skip if k and j are adjacent.
                    if _has_any_edge(dag, k, j):
                        continue
                    # Make i-j into i->j
                    _logger.debug('R1: remove edge (%s, %s)' % (j, i))
                    dag.remove_edge(j, i)
                    break

            # Rule 2: Orient i-j into i->j whenever there is a chain
            # i->k->j.
            #
            # Check if i-j.
            if _has_both_edges(dag, i, j):
                # Find nodes k where k is i->k.
                succs_i = set()
                for k in dag.successors(i):
                    if not dag.has_edge(k, i):
                        succs_i.add(k)
                # Find nodes j where j is k->j.
                preds_j = set()
                for k in dag.predecessors(j):
                    if not dag.has_edge(j, k):
                        preds_j.add(k)
                # Check if there is any node k where i->k->j.
                if len(succs_i & preds_j) > 0:
                    # Make i-j into i->j
                    _logger.debug('R2: remove edge (%s, %s)' % (j, i))
                    dag.remove_edge(j, i)

            # Rule 3: Orient i-j into i->j whenever there are two chains
            # i-k->j and i-l->j such that k and l are nonadjacent.
            #
            # Check if i-j.
            if _has_both_edges(dag, i, j):
                # Find nodes k where i-k.
                adj_i = set()
                for k in dag.successors(i):
                    if dag.has_edge(k, i):
                        adj_i.add(k)
                # For all the pairs of nodes in adj_i,
                for (k, l) in combinations(adj_i, 2):
                    # Skip if k and l are adjacent.
                    if _has_any_edge(dag, k, l):
                        continue
                    # Skip if not k->j.
                    if dag.has_edge(j, k) or (not dag.has_edge(k, j)):
                        continue
                    # Skip if not l->j.
                    if dag.has_edge(j, l) or (not dag.has_edge(l, j)):
                        continue
                    # Make i-j into i->j.
                    _logger.debug('R3: remove edge (%s, %s)' % (j, i))
                    dag.remove_edge(j, i)
                    break

            # Rule 4: Orient i-j into i->j whenever there are two chains
            # i-k->l and k->l->j such that k and j are nonadjacent.
            #
            # However, this rule is not necessary when the PC-algorithm
            # is used to estimate a DAG.

        if nx.is_isomorphic(dag, old_dag):
            break
        old_dag = dag.copy()

    return dag


# In[7]:


def stdmtx(X):
    """
    Convert Normal Distribution to Standard Normal Distribution
    Input:
        X: Each column is a variable
    Output:
        X: Standard Normal Distribution
    """
    means = X.mean(axis = 0)
    stds = X.std(axis = 0, ddof=1)
    X = X - means[np.newaxis, :]
    X = X / stds[np.newaxis, :]
    return np.nan_to_num(X)


# In[101]:


def nameMapping(df):
    ### Map integer to name
    mapping = {i: name for i, name in enumerate(df.columns)}
    return mapping
    
def plotgraph(g, mapping):
    g = nx.relabel_nodes(g, mapping)
    plt.figure(num=None, figsize=(18, 18), dpi=80)
    plt.axis('off')
    fig = plt.figure(1)
    pos = nx.shell_layout(g)
    nx.draw_networkx_nodes(g,pos)
    nx.draw_networkx_edges(g,pos)
    nx.draw_networkx_labels(g,pos)
    

def savegraph(gs, corr_matrix, mapping, edgeType):
    if len(gs) == 1:
        g = gs[0]
    else:
        from collections import Counter, OrderedDict
        edges_all = [e  for g in gs for e in list(g.edges)]
        edges_appear_count = Counter(edges_all)
        edges_keep = edges_all # [v  for v, num in edges_all.items() if num == MI_DATASET]
        g=nx.empty_graph(corr_matrix.shape[0],create_using=nx.DiGraph())
        g.add_edges_from(edges_keep)
    
    ### save edges to excel
    mapping_r = {name:i for i, name in mapping.items()}
    strength = []

    for i, j in g.edges:
        if edgeType == 's':
            if cuda:
                print(corr_matrix[i, j].cpu().item())
                strength.append(corr_matrix[i, j].cpu().item())
            else:
                print(corr_matrix[i, j].item())
                strength.append(corr_matrix[i, j].item())
        elif edgeType == 'c':
            strength.append(edges_appear_count[(i, j)])
    
        else:
            strength.append(np.nan)
            
    data = {'Cause': [mapping[e[0]] for e in g.edges], 'Effect': [mapping[e[1]] for e in g.edges], 'Strength': [round(a, 3) for a in strength]}
    graph_excel = pd.DataFrame.from_dict(data)
    graph_excel.to_excel("graph_excel.xlsx", index=False)
    


# In[102]:

def getblackList(blacklist, node_size):
    node_ids = range(node_size)
    init_graph = _create_complete_graph(node_ids)
    with open(blacklist, 'rb') as f:
        for line in f.readlines():
            cause, effect = line.splitlines()[0].decode("utf-8").split(',')
            i, j = df.columns.get_loc(cause.strip()), df.columns.get_loc(effect.strip())
            init_graph.remove_edge(i,j)
    return init_graph

# getblackList(blacklist)


# In[103]:


def getTiers(tiers, mapping_r):
    with open(tiers, 'rb') as f:
        timeinfodict = dict()
        n = 1
        for line in f.readlines():
            line = line.splitlines()[0].decode("utf-8").split(',')
            line = [mapping_r[i.strip()] for i in line]
            timeinfodict[n] = line
            n+=1
    return timeinfodict
# getTiers(tiers)


# In[104]:


def getknownedges(knownedges, mapping_r):
#     nonlocal  mapping_r
    know_edge_list = []
    with open(knownedges, 'rb') as f:
        for line in f.readlines():
            cause, effect = line.splitlines()[0].decode("utf-8").split(',')
            know_edge_list.append([mapping_r[cause.strip()], mapping_r[effect.strip()]])
    return know_edge_list
# getknownedges(knownedges)


# #### Generate Data

# ### PC algorithm

# In[106]:


# In[107]:

def main(df, alpha, cuda, knownEdgesFile, blackListFile, tiersFile, imputation, edgeType):
    mapping = {i: name for i, name in enumerate(df.columns)}
    mapping_r = {name:i for i, name in mapping.items()}

    def checkNull(imputation, edgeType):
        if df.isnull().values.any():
            txt = input("Dataframe contains missing value(s), do you want to perform Multiple Imputation? (Y/N)")
            if txt.strip() in ['Y', 'y']:
                imputation = True
                edgeType = 'c'
            elif txt.strip() in ['N', 'n']:
                sys.exit("Execution terminated: please fill missing data in the dataframe.")
            else:
                print('Invalid input')
                checkNull()
        return imputation, edgeType
                
    imputation, edgeType = checkNull(imputation, edgeType)
    
    ### Multiple Imputation
    datasets = []
    if imputation:
        kernel = mf.MultipleImputedKernel(
          data=df,
          datasets=MI_DATASET,
          save_all_iterations=True,
          random_state=1991,
          mean_match_candidates=5
        )

        # Run the MICE algorithm for 3 iterations on each of the datasets
        kernel.mice(1, verbose=True, n_jobs=2)

        datasets = []
        for i in range(MI_DATASET):
            datasets.append(pd.get_dummies( kernel.complete_data(i)))   # Categorical to continuous
    else:
        datasets.append(df)

    gs = []
    for df in datasets:
        N = df.shape[0]
        node_size = df.shape[1]

        corr_matrix = np.corrcoef(df.values.T)
        corr_matrix = torch.tensor(corr_matrix, dtype=torch.float32)
        
        if cuda:
            corr_matrix = corr_matrix.to(device)

        st = time.time()

        ### Blacklist
        if blackListFile:
            init_graph = getblackList(blackListFile, node_size)
        else:
            init_graph = _create_complete_graph(range(node_size))

        ### Tiers
        if tiersFile:
            timeInfoDict = getTiers(tiersFile, mapping_r)
        else:
            timeInfoDict = None

        ### knowngraphs
        if knownEdgesFile:
            know_edge_list = getknownedges(knownEdgesFile, mapping_r)   
        else:
            know_edge_list = []

        (g, sep_set) = estimate_skeleton(corr_matrix=corr_matrix,
                                             sample_size=N,
                                             alpha=alpha,
                                             init_graph=init_graph,
                                             know_edge_list=know_edge_list,
                                             method='stable')

        g = estimate_cpdag(skel_graph=g, sep_set=sep_set, timeInfoDict=timeInfoDict, know_edge_list=know_edge_list)

        en = time.time()
        print("Total running time:", en-st)
        print('Edges are:', g.edges(), end='')

        ### Integer to real name
        gs.append(g)
        plotgraph(g, mapping)
    
    savegraph(gs, corr_matrix, mapping, edgeType)


# In[108]:

# # Parameter setting
# alpha = 10**-10
# cuda = True
# imputation = False
# edgeType = 's' # 's': strength of correlation, c': confidence
# knownEdgesFile = None # 'data/knownedges.txt'
# blackListFile = None  # 'data/blacklist.txt'
# tiersFile = None # 'data/tiers.txt'
# MI_DATASET = 5

parser = argparse.ArgumentParser(description='fastPC: A Cuda-based Parallel PC Algorithm')

parser.add_argument('--significanceLevel', type=float, default=10**-6, help='Learning rate (default: 10^-6)')
parser.add_argument('--cuda', type=bool, default=False, help='Use CUDA (GPU) (default: False)')
parser.add_argument('--imputation', action="store_true", default=False, help='Use Multiple Imputation (default: False)')
parser.add_argument('--MI_DATASET', type=int, default=5, help='Number of Imputatation Dataset (default: 5)')
parser.add_argument('--edgeType', type=str, default='s', choices=['s', 'c'], help='Edge Type is correlation coefficient or confidence (default: correlation coefficient)')
parser.add_argument('data', help='(Path to) input dataset. Required file format: csv with each column as a random variable.')
parser.add_argument('--knownEdgesFile', nargs='?', help='(Path to) txt file containing known edges. Required file format: txt with a row (format: variable1, variable2) for each known directed edge: variable1 --> variable2.')
parser.add_argument('--blackListFile', nargs='?', help='(Path to) txt file containing edges should not appear. Required file format: txt with a row (format: variable1, variable2) for each known directed edge: variable1 --> variable2.')
parser.add_argument('--tiersFile', nargs='?', help='(Path to) txt file containing tiers in terms of time. Required file format: txt with a row (format: [variable1, variable2, variable3]) for each tier starting the earliest tiers.')

args = parser.parse_args()

print("Arguments:", args)

if torch.cuda.is_available():
    if not args.cuda:
        print('WARNING: You have a CUDA device, you should probably run with "--cuda True" to speed up training.')

# Parameter setting
alpha = args.significanceLevel
cuda = args.cuda
imputation = args.imputation
edgeType = args.edgeType # 's': strength of correlation, c': confidence
df = pd.read_excel(args.data)
if args.knownEdgesFile is not None:
    knownEdgesFile = args.knownEdgesFile
else:
    knownEdgesFile=None
if args.blackListFile is not None:
    blackListFile = args.blackListFile
else:
    blackListFile=None
if args.tiersFile is not None:
    tiersFile = args.tiersFile
else:
    tiersFile=None
# knownEdgesFile = None # 'data/knownedges.txt'
# blackListFile = None  # 'data/blacklist.txt'
# tiersFile = None # 'data/tiers.txt'
MI_DATASET = args.MI_DATASET

if cuda:
    device = torch.device(9)
    torch.cuda.set_device(device)
    torch.cuda.current_device()
    # torch.cuda.get_device_capability(device=None)

    
# df = pd.read_excel('data/sim_data.xlsx')
# print('df is', df)
main(df, alpha, cuda, knownEdgesFile, blackListFile, tiersFile, imputation, edgeType)
