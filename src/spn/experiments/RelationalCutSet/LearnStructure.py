'''
Created on August 16, 2018

@author: Alejandro Molina
'''

import glob

import pickle
import numpy as np
import os

from spn.algorithms.Inference import log_likelihood
from spn.algorithms.Marginalization import marginalize
from spn.algorithms.Validity import is_valid
from spn.structure.Base import Sum, Product, assign_ids, rebuild_scopes_bottom_up
from spn.structure.leaves.parametric.Parametric import CategoricalDictionary


class Dependency:
    def __init__(self, name):
        self.name = name
        self.children = []
        self.parents = []

    @staticmethod
    def parse(txt):
        from lark import Lark
        grammar = r"""
                    %import common.WS
                    %ignore WS
                    %import common.WORD -> WORD
                    node: "(" WORD ")"
                        | "(" WORD node ("," node)* ")"
                    """
        tree = Lark(grammar, start='node').parse(txt)

        def parse_tree(tree, parents=None):
            result = Dependency(str(tree.children[0]))
            if parents is not None:
                result.parents = parents
            for c in tree.children[1:]:
                child = parse_tree(c, parents=result.parents + [result])
                result.children.append(child)
            return result

        return parse_tree(tree)

    def __repr__(self):
        txt = ("%s %s" % (self.name, ",".join(map(str, self.children)))).strip()
        return "(%s)" % txt


def parse_attributes(path):
    attributes_in_table = {}
    scopes = {}
    meta_data = {}
    with open(path + "attributes.csv", "r") as attfile:
        for line in attfile:
            table = line.strip().split(':')
            attributes = table[1].split(',')
            table_name = table[0]
            meta_data[table_name] = {key: index for index, key in enumerate(attributes)}

            for att in attributes:
                if att not in scopes:
                    scopes[att] = len(scopes)

                if att not in attributes_in_table:
                    attributes_in_table[att] = set()
                attributes_in_table[att].add(table_name)
    return (attributes_in_table, scopes, meta_data)


def load_tables(path, debug=False):
    tables = {}
    for fname in glob.glob(path + "*.tbl"):
        table_name = os.path.splitext(os.path.basename(fname))[0]
        print("loading", table_name, "from", fname)
        tables[table_name] = np.loadtxt(fname, delimiter='|', usecols=range(0, len(meta_data[table_name])))

    # if in debug mode, reduce size
    if debug:
        tables["Ratings"] = tables["Ratings"][0:25]

        cond = np.zeros(tables["Users"].shape[0])
        for ruid in tables["Ratings"][:, 0]:
            cond = np.logical_or(cond, tables["Users"][:, 0] == ruid)
        tables["Users"] = tables["Users"][cond, :]

        cond = np.zeros(tables["Movies"].shape[0])
        for ruid in tables["Ratings"][:, 1]:
            cond = np.logical_or(cond, tables["Movies"][:, 0] == ruid)
        tables["Movies"] = tables["Movies"][cond, :]

    return tables


def get_values(attributes_in_table, meta_data, tables, att_name, constraints, table_row_idxs=None):
    result_set = None
    value_count = {}
    filtered_table_row_idxs_by_value = {}
    for table_name in attributes_in_table[att_name]:
        table_meta_data = meta_data[table_name]
        att_pos = table_meta_data[att_name]
        table = tables[table_name]

        # this is optimization
        row_mask = None
        row_idx = None
        if table_row_idxs is not None and table_name in table_row_idxs:
            row_idx = table_row_idxs[table_name]
            row_mask = row_idx > -1
        if row_mask is None:
            row_mask = np.ones(table.shape[0]) == 1
            row_idx = np.array(list(range(table.shape[0])))

        # this computes the constraints
        for constraint_attribute, constraint_value in constraints.items():
            constraint_att_pos = table_meta_data.get(constraint_attribute, None)
            if constraint_att_pos is None:
                continue
            row_mask = row_mask & (table[row_idx, constraint_att_pos] == constraint_value)

        # data contains the column with all the values we want, after filtering
        data = table[row_idx[row_mask], att_pos]

        # obtain unique values
        unique, rev_idx, count = np.unique(data, return_counts=True, return_inverse=True)
        for i, val in enumerate(unique):

            # this is optimization, to not fully scan all tables always
            table_row_idx_by_val = filtered_table_row_idxs_by_value.get(val, None)
            if table_row_idx_by_val is None:
                table_row_idx_by_val = {}
                filtered_table_row_idxs_by_value[val] = table_row_idx_by_val
            table_row_idx = table_row_idx_by_val.get(table_name, None)
            if table_row_idx is None:
                table_row_idx_by_val[table_name] = row_idx[rev_idx == i]

            # how many instances were found on the join?
            if val not in value_count:
                value_count[val] = count[i]
            else:
                value_count[val] = max(value_count[val], count[i])

        # we are only interested in the values that are intersected on all tables
        table_values = set(unique)
        if result_set is None:
            result_set = table_values
        else:
            result_set.intersection_update(table_values)

    # return the values, the counts, and the row indexes where those values are found
    for val in result_set:
        yield (val, value_count[val], filtered_table_row_idxs_by_value[val])


def build_csn(attributes_in_table, meta_data, tables, dependency_node, keys_per_attribute, ancestors, constraints=None,
              table_row_idxs=None, cache=None,
              debug=False):
    if constraints is None:
        constraints = {}

    att_name = dependency_node.name

    key = keys_per_attribute[att_name]
    can_cache = tuple(ancestors[att_name]) != tuple(key)

    if can_cache:
        cache_key = tuple([(k, constraints[k]) for k in key])
        new_node = cache.get(cache_key, None)
        if new_node is not None:
            return new_node

    new_node = Sum()

    new_constraints = dict(constraints)

    for val, count, filtered_table_row_idxs in get_values(attributes_in_table, meta_data, tables, att_name,
                                                          constraints, table_row_idxs):
        if debug:
            print("att_name", att_name, val)
        new_node.weights.append(count)

        p_node = Product()
        p_node.children.append(CategoricalDictionary(p={float(val): 1.0}, scope=scopes[att_name]))

        for dep_node in dependency_node.children:
            new_constraints[att_name] = val
            p_node.children.append(
                build_csn(attributes_in_table, meta_data, tables, dep_node, keys_per_attribute, ancestors,
                          constraints=new_constraints,
                          table_row_idxs=filtered_table_row_idxs,
                          cache=cache,
                          debug=False))

        new_node.children.append(p_node)

    wsum = np.sum(new_node.weights)
    new_node.weights = [w / wsum for w in new_node.weights]

    if can_cache:
        cache[cache_key] = new_node

    return new_node


def get_keys(dep_tree, meta_data, attributes_in_table):
    # keys are the attributes that show up in more than one table.
    keys = set()
    keys_per_table = {}
    for att, table_names in attributes_in_table.items():
        if len(table_names) > 1:
            keys.add(att)
            for table_name in table_names:
                if table_name not in keys_per_table:
                    keys_per_table[table_name] = []
                keys_per_table[table_name].append(att)

    keys_per_attribute = {}
    ancestors = {}

    def process_dep_tree(dep_node):
        att_name = dep_node.name

        ancestors[att_name] = set(list(map(lambda d: d.name, dep_node.parents)))
        tables = attributes_in_table[att_name]

        if len(dep_node.parents) == 0:
            # i'm root, I have no parents
            keys_per_attribute[att_name] = []
        elif len(tables) == 1:
            # i belong to only one table
            table = [t for t in tables][0]
            table_atts = meta_data[table]
            keys_per_attribute[att_name] = [att for att in ancestors[att_name] if att in table_atts]
        else:
            # i belong to multiple tables, use the one that has the attribute with my parent
            table_atts = meta_data[[t for t in tables if dep_node.parents[-1].name in meta_data[t]][0]]
            keys_per_attribute[att_name] = [att for att in ancestors[att_name] if att in table_atts]

        for c in dep_node.children:
            process_dep_tree(c)

    process_dep_tree(dep_tree)
    return keys_per_attribute, ancestors


def cluster_ids(tables, attributes_in_table):

    pass


if __name__ == '__main__':
    path = "/Users/alejomc/Downloads/100k/"

    with open(path + "dependencies.txt", "r") as depfile:
        dep_tree = Dependency.parse(depfile.read())

    print(dep_tree)

    attributes_in_table, scopes, meta_data = parse_attributes(path)

    keys_per_attribute, ancestors = get_keys(dep_tree, meta_data, attributes_in_table)

    tables = load_tables(path, debug=False)

    tables = cluster_ids(tables, attributes_in_table)

    spn = None

    file_cache_path = "/tmp/csn.bin"
    if not os.path.isfile(file_cache_path):
        spn = build_csn(attributes_in_table, meta_data, tables, dep_tree, keys_per_attribute, ancestors, cache={},
                        debug=True)
        rebuild_scopes_bottom_up(spn)
        print(spn)
        assign_ids(spn)
        print(is_valid(spn))

        keep = set(scopes.values())
        keep.discard(scopes["userid"])
        keep.discard(scopes["movieid"])

        marg = marginalize(spn, keep)
        with open(file_cache_path, 'wb') as f:
            pickle.dump((spn, marg), f, pickle.HIGHEST_PROTOCOL)
    else:
        print("loading cached spn")
        with open(file_cache_path, 'rb') as f:
            spn, marg = pickle.load(f)
        print("loaded cached spn")


    def to_data(scopes, **kwargs):
        data = np.zeros((1, max(scopes.values()) + 1))
        data[:] = np.nan
        for k, v in kwargs.items():
            data[0, scopes[k]] = v
        return data


    def compute_conditional(spn, scopes, query, **evidence):
        q_e = dict(query)
        q_e.update(evidence)
        query_str = ",".join(map(lambda t: "%s=%s"%(t[0],t[1]), query.items()))
        evidence_str = ",".join(map(lambda t: "%s=%s"%(t[0],t[1]), evidence.items()))
        prob_str = "P(%s|%s)" % (query_str, evidence_str )
        #print("computing ", prob_str)

        a = log_likelihood(spn, to_data(scopes, **q_e), debug=False)
        #print("query ", query_str, np.exp(a))
        b = log_likelihood(spn, to_data(scopes, **evidence), debug=False)
        #print("evidence ", evidence_str, b, np.exp(b))
        result = np.exp(a - b)
        print(prob_str, "=", result, "query ", query_str, np.exp(a), evidence_str, np.exp(b))
        return result


    compute_conditional(spn, scopes, {'rating': 5}, age=25.0, occupation=3.0)
    compute_conditional(spn, scopes, {'rating': 5}, fantasy=1.0, romance=1.0)
    compute_conditional(spn, scopes, {'rating': 5}, fantasy=1.0, romance=1.0, age=25.0)
    compute_conditional(spn, scopes, {'rating': 3}, fantasy=1.0, romance=1.0, age=25.0)
    compute_conditional(spn, scopes, {'rating': 3}, crime=1.0, occupation=4.0, age=25.0)