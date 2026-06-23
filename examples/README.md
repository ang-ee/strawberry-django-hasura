# examples — the stock refine provider drives the adapter, unmodified

This directory is a runnable proof that the **stock**
[`@refinedev/hasura`](https://refine.dev/docs/data/packages/hasura/) data
provider drives a `strawberry-django-hasura` schema with **no patching** and no
custom mapping — using only the documented `idType: "String"` option.

- `demo_schema.py` — a self-contained toy `Note` model + a Hasura-shaped
  Strawberry schema built with this library (the `notes` list with where /
  order_by / limit / offset, the free `notes_aggregate { aggregate, nodes }`,
  `notes_by_pk`, and `insert`/`update`/`delete`-by-pk). The public `id` is an
  opaque sqid. Mirrors `tests/demo_schema.py`.
- `server.py` — a minimal single-threaded `http.server` serving
  `schema.execute_sync` over an in-memory SQLite DB. A real backend serves the
  same schema through an ASGI/GraphQL view; this is the smallest thing that
  proves the wire contract.
- `refine-client/client_test.cjs` — a Node script that constructs the stock
  provider as `dataProvider(new GraphQLClient(url), { idType: "String",
  namingConvention: "hasura-default" })` and exercises `getList` (where + sort +
  paging), `getOne`, `create`, `update`, `deleteOne`, and the aggregate (via the
  provider's custom/gqlQuery path), asserting the results — 13/13.

## Run

```sh
# 1. serve the toy Hasura Note schema (from the repo root)
uv run python examples/server.py &

# 2. install the stock client + run the proof (in a scratch dir)
mkdir -p /tmp/refine-client && cd /tmp/refine-client
echo '{"type":"module","private":true}' > package.json
pnpm add @refinedev/hasura graphql graphql-request graphql-tag
node /path/to/strawberry-django-hasura/examples/refine-client/client_test.cjs
```

## Notes

- The provider is `@refinedev/hasura` (v7.0.1) + `graphql-request@7`, used
  exactly as the documented stock setup — no fork, no patch. The one required
  option is `idType: "String"` so the opaque sqid `id` binds as `$id: String!`
  (refine's default `idType` is `uuid`).
- **Use the CJS build** of the test (`client_test.cjs`): the provider's ESM build
  mis-imports a `lodash` path that strict Node ESM rejects; `require(...)` (CJS)
  is fine, and in a real Vite/bundler frontend this is a non-issue (the bundler
  resolves it).
- `server.py` is a demo transport. In production the same schema is served
  through your project's ASGI/GraphQL view.
