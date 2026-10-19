"""Merge declaration identities and structural relationships without new symbols."""
from collections import Counter, defaultdict
from dataclasses import replace
from .rebuild import rows, build


class StructuralFacts:
    def __init__(self, work):
        self.w = work
        self.declarations = defaultdict(set)
        self.inheritance = defaultdict(set)
        self.overrides = set()
        self.eligibility = defaultdict(set)
        self.counts = Counter()

    def accept(self, context, result, valid):
        w = self.w
        for d in result.declarations:
            identifier = w.identifier(d.usr)
            if identifier is None or d.path not in valid:
                continue
            locations = {}
            for field in ('declaration', 'definition'):
                extent = getattr(d, field)
                if extent:
                    try:
                        if extent.path not in valid:
                            raise ValueError('Unverified declaration location')
                        w.coordinates(extent)
                    except ValueError as error:
                        w.issue(context, d.path, str(error), 'symbols')
                        extent = None
                locations[field] = extent
            self.declarations[identifier].add(replace(d, **locations))
            self.eligibility[identifier].add(d.eligibility)
        for edge in result.inheritance:
            if edge.path in valid:
                self.inheritance[edge.base_usr, edge.derived_usr, edge.path].add(
                    (edge.access, edge.virtual, edge.existing_edge_only))
        for edge in result.overrides:
            if edge.path in valid:
                self.overrides.add(edge)

    def apply(self):
        w = self.w
        # Allocate source locations before replaying the canonical graph.
        for declarations in self.declarations.values():
            for d in declarations:
                for extent in (d.declaration, d.definition):
                    if extent:
                        w.location(extent)
        data = rows(w.builder.build())
        symbols = {r['symbol_id']: r for r in data['symbols']}
        scopes = {r['scope_id']: r for r in data['scopes']}
        desired = {}
        paths = {}

        def vocabulary(collection, id_field, name):
            for r in data[collection]:
                if r['name'] == name:
                    return r[id_field]
            identifier = max((r[id_field] for r in data[collection]), default=0) + 1
            data[collection].append({id_field: identifier, 'name': name})
            return identifier

        def conflict(symbol, field):
            self.counts['conflicts'] += 1
            w.issue('', paths[symbol], f'Conflicting AST {field} for symbol {symbol}', 'symbols')

        def consensus(symbol, declarations, field, nonempty=False):
            values = {getattr(d, field) for d in declarations}
            if nonempty:
                values.discard('')
                values.discard(None)
            if len(values) > 1:
                conflict(symbol, field)
                return None
            return next(iter(values), None)

        for identifier, declarations in sorted(self.declarations.items()):
            paths[identifier] = min(d.path for d in declarations)
            row = symbols[identifier]
            w.matched.add(identifier)
            parent = consensus(identifier, declarations, 'parent_usr')
            scope = consensus(identifier, declarations, 'scope')
            if row['name'] is None:
                required = {field: consensus(identifier, declarations, field) for field in ('name', 'kind', 'language')}
                if any(value is None for value in required.values()) or scope is None or parent is None:
                    continue
                row['name'] = required['name']
                row['kind_id'] = vocabulary('symbol_kinds', 'kind_id', required['kind'])
                row['language_id'] = vocabulary('languages', 'language_id', 'C++' if required['language'] == 'Cpp' else 'C')
                row['scope_id'] = 1  # Replaced below for non-global semantic contexts.
                self.counts['placeholders_resolved'] += 1
            if parent is not None and scope is not None:
                old_owner = scopes[row['scope_id']]['owner_symbol_id']
                new_owner = w.identifier(parent) if parent else None
                if new_owner == identifier or (old_owner is not None and old_owner != new_owner):
                    conflict(identifier, 'semantic parent')
                else:
                    desired[identifier] = (parent, scope)
            if row['kind_id'] is None:
                kind = consensus(identifier, declarations, 'kind', True)
                if kind:
                    row['kind_id'] = vocabulary('symbol_kinds', 'kind_id', kind)
                    self.counts['symbol_fields_filled'] += 1
            for field, source in (('signature', 'signature'), ('return_type', 'return_type'),
                                  ('documentation', 'documentation'), ('template_specialization_args', 'template_arguments')):
                value = consensus(identifier, declarations, source, True)
                if value:
                    if row[field] is None:
                        row[field] = value
                        self.counts['symbol_fields_filled'] += 1
                    elif row[field] != value:
                        conflict(identifier, field)
            for field in ('declaration', 'definition'):
                if row[field + '_location_id'] is not None:
                    continue
                extents = {getattr(d, field) for d in declarations if getattr(d, field)}
                # Multiple redeclarations are legitimate; select a stable name location.
                if extents:
                    extent = min(extents, key=lambda e: (e.path, e.start, e.end))
                    row[field + '_location_id'] = w.location(extent)
                    self.counts['symbol_fields_filled'] += 1

        # Identity-verified owners permit consolidation; names alone never do.
        owner_for = {}
        for symbol, (parent, scope) in desired.items():
            owner = w.identifier(parent) if parent else None
            if owner is not None and symbols[owner]['name'] is None:
                owner = None
            owner_for[symbol] = owner
            if parent and owner is None:
                w.issue('', paths[symbol], f'Unresolved semantic parent for symbol {symbol}', 'symbols')
        # Reject cyclic proposed ownership without discarding unrelated enrichment.
        parents = {identifier: scopes[row['scope_id']]['owner_symbol_id']
                   for identifier, row in symbols.items() if row['scope_id'] is not None}
        parents.update(owner_for)
        cyclic = set()
        for start in owner_for:
            chain, positions = [], {}
            current = start
            while current is not None and current not in positions:
                positions[current] = len(chain)
                chain.append(current)
                current = parents.get(current)
            if current in positions:
                cyclic.update(chain[positions[current]:])
        for identifier in cyclic & owner_for.keys():
            conflict(identifier, 'cyclic semantic parent')
            owner_for[identifier] = None
        users = defaultdict(set)
        for row in symbols.values():
            if row['scope_id'] is not None:
                users[row['scope_id']].add(row['symbol_id'])
        by_owner = defaultdict(list)
        for symbol, owner in owner_for.items():
            if owner is not None:
                by_owner[owner].append(symbol)
        redirects = {}
        for owner, children in sorted(by_owner.items()):
            candidates = [scope_id for scope_id, row in scopes.items() if scope_id != 1 and
                (row['owner_symbol_id'] == owner or
                 (row['owner_symbol_id'] is None and users[scope_id] and
                  all(owner_for.get(s) == owner for s in users[scope_id])))]
            if candidates:
                canonical = min(candidates)
            else:
                canonical = max(scopes) + 1
                scopes[canonical] = {'scope_id': canonical, 'qualified_name': desired[min(children)][1] or '(anonymous)::',
                                     'owner_symbol_id': None}
            if scopes[canonical]['owner_symbol_id'] is None:
                self.counts['scope_owners_filled'] += 1
            scopes[canonical]['owner_symbol_id'] = owner
            for old in candidates:
                if old != canonical:
                    redirects[old] = canonical
            for child in children:
                symbols[child]['scope_id'] = canonical
        for symbol, (parent, scope) in desired.items():
            if not parent:
                symbols[symbol]['scope_id'] = 1
            elif owner_for[symbol] is None and symbols[symbol]['scope_id'] == 1:
                identifier = max(scopes) + 1
                scopes[identifier] = {'scope_id': identifier, 'qualified_name': scope or '(anonymous)::', 'owner_symbol_id': None}
                symbols[symbol]['scope_id'] = identifier
        for row in symbols.values():
            row['scope_id'] = redirects.get(row['scope_id'], row['scope_id'])
        data['scopes'] = [row for key, row in sorted(scopes.items()) if key not in redirects]

        def endpoint(usr, path):
            identifier = w.identifier(usr)
            if identifier is None or symbols[identifier]['name'] is None:
                w.issue('', path, f'Unresolved relation endpoint: {usr}', 'symbols')
                return None
            return identifier

        existing = {(r['base_symbol_id'], r['derived_symbol_id']): r for r in data['class_inheritance']}
        original_edges = set(existing)
        for (base, derived, path), values in sorted(self.inheritance.items()):
            base_id, derived_id = endpoint(base, path), endpoint(derived, path)
            if base_id is None or derived_id is None or base_id == derived_id:
                continue
            if (base_id, derived_id) not in original_edges and any(only for _, _, only in values):
                w.issue('', path, f'Template base has no existing inheritance edge: {base} -> {derived}', 'symbols')
            values = {(access, virtual) for access, virtual, only in values
                      if not only or (base_id, derived_id) in original_edges}
            if not values:
                continue
            if len(values) != 1:
                self.counts['conflicts'] += 1
                w.issue('', path, 'Conflicting inheritance attributes across contexts', 'symbols')
                continue
            access, virtual = next(iter(values))
            access_id = vocabulary('access_specifiers', 'access_id', access)
            row = existing.get((base_id, derived_id))
            if row is None:
                row = {'inheritance_id': max((r['inheritance_id'] for r in data['class_inheritance']), default=0)+1,
                       'base_symbol_id': base_id, 'derived_symbol_id': derived_id, 'access_id': None, 'is_virtual': None}
                data['class_inheritance'].append(row)
                existing[base_id, derived_id] = row
            changed = False
            for key, value in (('access_id', access_id), ('is_virtual', virtual)):
                if row[key] is None:
                    row[key] = value
                    changed = True
                elif row[key] != value:
                    self.counts['conflicts'] += 1
                    w.issue('', path, f'Conflicting inheritance {key}', 'symbols')
            self.counts['inheritance_enriched'] += changed
        existing = {(r['overridden_symbol_id'], r['overriding_symbol_id']) for r in data['method_overrides']}
        for edge in sorted(self.overrides, key=lambda e: (e.base_usr, e.derived_usr, e.path)):
            base, derived = endpoint(edge.base_usr, edge.path), endpoint(edge.derived_usr, edge.path)
            if base is None or derived is None or base == derived or (base, derived) in existing:
                continue
            data['method_overrides'].append({'override_id': max((r['override_id'] for r in data['method_overrides']), default=0)+1,
                'overridden_symbol_id': base, 'overriding_symbol_id': derived})
            existing.add((base, derived))
            self.counts['overrides_added'] += 1
        result = build(w.model.metadata, data)
        return result
