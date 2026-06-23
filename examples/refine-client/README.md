# refine-client — stock `@refinedev/hasura` proof

`client_test.cjs` drives the toy `Note` endpoint with the **unmodified** refine
Hasura data provider (`getList` where + sort + paging, `getOne`, `create`,
`update`, `deleteOne`, and the aggregate) and asserts the results — no provider
patching, no custom mapping. The sqid `id` roundtrips through every pk-centric
op with only the `idType: "String"` provider option.

See [`../README.md`](../README.md) for how to start the server and run this
script.
