import logging
from functools import cached_property
from typing import Dict, List, Set, cast

import networkx as nx
from google.protobuf.json_format import MessageToDict
from tqdm import tqdm

from automata.tools.search.scip_pb2 import Index, SymbolRole
from automata.tools.search.symbol_converter import SymbolConverter
from automata.tools.search.symbol_parser import parse_symbol
from automata.tools.search.symbol_types import File, PyPath, StrPath, Symbol, SymbolReference

logger = logging.getLogger(__name__)


class SymbolGraph:
    def __init__(self, index_path: str, symbol_converter: SymbolConverter):
        """
        Initialize SymbolGraph with the path of an index protobuf file.

        :param index_path: Path to index protobuf file
        :param symbol_converter: SymbolConverter instance
        :param do_shortened_symbols: Whether to use shortened symbols

        :return: SymbolGraph instance
        """
        self.converter = symbol_converter

        self._index = self._load_index_protobuf(index_path)
        self._graph = self._build_symbol_graph(self._index)

    def get_all_files(self) -> List[File]:
        """
        Get all file nodes in the graph.
        :param data_type: Optional filter for data type

        :return: List of all file nodes.
        """
        return [
            data.get("file")
            for _, data in self._graph.nodes(data=True)
            if data.get("label") == "file"
        ]

    def get_all_defined_symbols(self) -> List[Symbol]:
        """
        Get all file nodes in the graph.
        :param data_type: Optional filter for data type

        :return: List of all defined symbols.
        """
        return [
            node for node, data in self._graph.nodes(data=True) if data.get("label") == "symbol"
        ]

    def get_symbol_references(self, symbol: Symbol) -> Dict[StrPath, List[SymbolReference]]:
        """
        Finds all references of a given symbol in the symbol graph.

        :param symbol: Symbol to search for
        :return: List of tuples (file, symbol details)
        """
        search_results = [
            (file_path, data.get("symbol_reference"))
            for _, file_path, data in self._graph.out_edges(symbol, data=True)
            if data.get("label") == "reference"
        ]
        result_dict: Dict[StrPath, List[SymbolReference]] = {}

        for file_path, symbol_reference in search_results:
            if file_path in result_dict:
                result_dict[file_path].append(symbol_reference)
            else:
                result_dict[file_path] = [symbol_reference]

        return result_dict

    def get_all_references_in_module(self, module) -> List[SymbolReference]:
        reference_edges_in_module = self._graph.in_edges(module, data=True)
        result = []
        for source, _, data in reference_edges_in_module:
            if data["label"] == "reference":
                result.append(data.get("symbol_reference"))

        return result

    def get_defined_symbols_along_path(self, partial_py_path: PyPath) -> Set[Symbol]:
        """
        Gets all symbols which contain a specified partial path

        :param partial_path: The partial_path to explain
        :return: Set of symbols that follow the partial path
        """
        obs_symbols = set()
        symbols = self.get_all_defined_symbols()
        for symbol in symbols:
            if partial_py_path in symbol.uri:
                obs_symbols.add(symbol)
        return obs_symbols

    def get_symbol_context(self, symbol) -> str:
        """
        Get the context for a given symbol. This includes its documentation, its file, references, and relationships.

        :param symbol: The symbol to explain
        :return: A string containing the explanation
        """

        result = ""
        docs = ["\n"]

        result += "Symbol Context for %s --> \n" % (symbol)
        doc_decorator = "  >> Symbol Docs       --> "
        spacer = " " * (0 + len(doc_decorator))
        result += f"{doc_decorator}%s\n" % (("\n" + spacer).join(docs)) + "\n"

        containing_file = self.get_parent_file(symbol)
        result += " >>  Symbol File       --> %s \n" % containing_file

        references = self.get_symbol_references(symbol)
        result += " >>  Symbol Ref Count  --> %s \n" % len(references.keys())

        symbol_references = "\n"
        for occurrence_file, outputs in references.items():
            for output in outputs:
                line_number = output.line_number
                roles = str(list(output.roles.keys()))
                symbol_references += f"{spacer}{occurrence_file}: L{line_number}, {roles} \n"

        result += " >>  Symbol Refs (All) --> %s" % (symbol_references)

        return result

    def _build_symbol_graph(self, index: Index) -> nx.MultiDiGraph:
        """
        Build a multidirectional graph from a given index.

        :param index: The index from which the graph is to be built.
        :return: The built multidirectional graph.
        """
        G = nx.MultiDiGraph()
        for document in index.documents:
            # Add FilePath Vertices
            document_path: StrPath = cast(StrPath, document.relative_path)

            G.add_node(
                document_path,
                file=File(document_path, occurrences=document.occurrences),
                label="file",
            )

            for symbol_information in document.symbols:
                symbol = parse_symbol(symbol_information.symbol)
                # Add Symbol Vertices
                G.add_node(
                    symbol,
                    label="symbol",
                )
                G.add_edge(document_path, symbol, label="contains")
        # process occurrences and relationships
        for document in index.documents:
            information_document_path: StrPath = cast(StrPath, document.relative_path)
            for symbol_information in document.symbols:
                symbol = parse_symbol(symbol_information.symbol)

                for relationship in symbol_information.relationships:
                    relationship_labels = MessageToDict(relationship)
                    relationship_labels.pop("symbol")
                    related_symbol = parse_symbol(relationship.symbol)
                    G.add_edge(
                        symbol,
                        related_symbol,
                        label="relationship",
                        **relationship_labels,
                    )

            for occurrence in document.occurrences:
                occurrence_symbol = parse_symbol(occurrence.symbol)
                occurrence_range = tuple(occurrence.range)
                occurrence_roles = self._get_symbol_roles_dict(occurrence.symbol_roles)
                occurrence_reference = SymbolReference(
                    symbol=occurrence_symbol,
                    line_number=occurrence_range[0],
                    column_number=occurrence_range[1],
                    roles=occurrence_roles,
                )
                G.add_edge(
                    occurrence_symbol,
                    information_document_path,
                    symbol_reference=occurrence_reference,
                    label="reference",
                )
                if occurrence_roles.get(SymbolRole.Name(SymbolRole.Definition)):
                    # TODO this is gross
                    incorrect_contains_edges = [
                        (source, target)
                        for source, target, data in G.in_edges(occurrence_symbol, data=True)
                        if data.get("label") == "contains"
                    ]
                    for source, target in incorrect_contains_edges:
                        G.remove_edge(source, target)

                    G.add_edge(
                        information_document_path,
                        occurrence_symbol,
                        label="contains",
                    )

        return G

    def get_parent_file(self, symbol: Symbol) -> str:
        parent_file_list = [
            source
            for source, _, data in self._graph.in_edges(symbol, data=True)
            if data.get("label") == "contains"
        ]
        assert (
            len(parent_file_list) == 1
        ), f"{symbol.uri} should have exactly one parent file, but has {len(parent_file_list)}"
        return parent_file_list.pop()

    def get_symbols_in_scope(self, symbol: Symbol) -> Set[Symbol]:
        """
        Returns the list of symbols referenced inside the scope of the given symbol (including children scopes).

        Args:
            symbol

        Returns:
            Set[Symbol]: The list of dependencies for the symbol.
        """
        references_in_range = self._get_references_in_scope(symbol)
        symbols_in_range = set([ref.symbol for ref in references_in_range])
        return symbols_in_range

    def get_relationship_symbols(self, symbol: Symbol) -> Set[Symbol]:
        """
        Returns the list of symbols with relationships to the given symbol.
        """
        related_symbol_nodes = set(
            [
                target
                for _, target, data in self._graph.out_edges(symbol, data=True)
                if data.get("label") == "relationship"
            ]
        )
        return related_symbol_nodes

    @cached_property
    def symbol_to_symbol_subgraph(self) -> nx.DiGraph:
        """
        Returns a subgraph with symbols mapped to outher symbols.

        Args:
            symbol (str): The symbol in the form 'module`/ClassOrMethod#'

        Returns:
            List[str]: The list of dependencies for the symbol.
        TODO: this is extremely slow
        TODO: this has a number of edge cases that are not handled in obvious ways
        TODO: see is_worth_backlinking_to for a list of things I chose to exclude
        """
        G = nx.DiGraph()

        # def is_worth_backlinking_to(symbol: Symbol):  # TODO this should not be necessary
        #     """
        #     Returns true if the symbol is valid for the purposes of the symbol to symbol subgraph.
        #     Valid symbols are "primary" nodes in the sense that we will find their dependencies and point from them to the node.
        #     Only valid symbols will have backlinks (and thus receive additional PageRank influence from their complexity)

        #     A list of things I chose to exclude:
        #     - protobuf generated objects: this is self-explanatory but their complexity shouldn't uprank them
        #         because they're autogenerated
        #     - local symbols: these are internally scoped and are not very meaningful
        #         for understanding the codebase as a whole
        #     - meta symbols: these, as far as I can tell, are only used for __init__.py files, which are not very meaningful
        #     - parameters: they should not have backlinks anyway
        #     - external libraries and dependencies: this will just make the graph too big without adding much value

        #     :param symbol:
        #     :return: bool
        #     """
        #     return (
        #         symbol.package.name == "automata"
        #         and symbol.module_name.startswith("automata")  # TODO: constant
        #         and not Symbol.is_protobuf(symbol)
        #         and not Symbol.is_local(symbol)
        #         and not Symbol.is_meta(symbol)
        #         and not Symbol.is_parameter(symbol)
        #     )

        # all_symbols = [
        #     symbol for symbol in self.get_all_defined_symbols() if is_worth_backlinking_to(symbol)
        # ]
        from automata.tools.search.symbol_types import Descriptor

        def _filter_symbols(
            symbols: List[Symbol],
            filter_strings=["__init__", "setup", "local", "test"],
            accepted_kinds=[Descriptor.PythonKinds.Method, Descriptor.PythonKinds.Class],
        ) -> List[Symbol]:
            """
            Filter out symbols that are not relevant for the embedding map.

            Args:
                symbols: List of symbols to filter
            Returns:
                List of filtered symbols
            """
            filtered_symbols = []

            for symbol in symbols:
                do_continue = False
                for filter_string in filter_strings:
                    if filter_string in symbol.uri:
                        do_continue = True
                        break
                if do_continue:
                    continue

                symbol_kind = symbol.symbol_kind_by_suffix()
                if symbol_kind not in accepted_kinds:
                    continue
                filtered_symbols.append(symbol)
            return filtered_symbols

        filtered_symbols = _filter_symbols(self.get_all_defined_symbols())

        for symbol in tqdm(filtered_symbols):
            try:
                dependencies = self.get_symbols_in_scope(symbol)
                relationships = self.get_relationship_symbols(symbol)
                for node in dependencies.union(relationships):
                    G.add_edge(node, symbol)
            except Exception as e:
                print(f"Error processing {symbol.uri}: {e}")

        return G

    def _get_references_in_scope(self, symbol: Symbol) -> List[SymbolReference]:
        """
        Returns the list of symbols referenced inside the scope of the given symbol (including children scopes).
        """
        module = self.get_parent_file(symbol)
        if module not in self.converter._module_dict:  # TODO
            return []
        fst_object = self.converter.convert_to_fst_object(symbol)

        # RedBaron POSITIONS ARE 1 INDEXED AND SCIP ARE 0!!!!
        parent_symbol_start_line, parent_symbol_start_col, parent_symbol_end_line = (
            fst_object.absolute_bounding_box.top_left.line - 1,
            fst_object.absolute_bounding_box.top_left.column - 1,
            fst_object.absolute_bounding_box.bottom_right.line - 1,
        )

        references_in_parent_module = self.get_all_references_in_module(module)
        references_in_range = [
            ref
            for ref in references_in_parent_module
            if parent_symbol_start_line <= ref.line_number < parent_symbol_end_line
            and ref.column_number >= parent_symbol_start_col
            and ref.symbol != symbol  # TODO: don't include self (or maybe do?)
            and "stdlib"
            not in ref.symbol.package.name  # TODO: don't include stdlib (or maybe do?)
            and not Symbol.is_local(ref.symbol)  # TODO: figure out how local symbols really work
            and not Symbol.is_meta(ref.symbol)  # TODO: figure out how meta symbols really work
        ]

        return references_in_range

    @staticmethod
    def _get_symbol_roles_dict(role) -> Dict[str, bool]:
        """
        Get a dictionary of symbol roles from a role Bitset.

        :param role: Role Bitset
        :return: A dictionary of symbol roles
        """
        result = {}
        for role_name, role_value in SymbolRole.items():
            if (role & role_value) > 0:
                result[role_name] = (role & role_value) > 0
        return result

    @staticmethod
    def _load_index_protobuf(path: StrPath) -> Index:
        """
        Load an index from a protobuf file.

        :param path: The path of the protobuf file
        :return: The loaded index
        """
        index = Index()
        with open(path, "rb") as f:
            index.ParseFromString(f.read())
        return index
