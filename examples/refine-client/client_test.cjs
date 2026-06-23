// Proof that the STOCK @refinedev/hasura data provider drives a
// strawberry-django-hasura schema with NO patching. Talks to ../server.py at
// :8099 (run it first — see ../README.md).
//
// Proves three things:
//  (1) stock provider binds unpatched (getList/getOne/create/update/deleteOne)
//  (2) sqid roundtrips through the pk-centric ops (by_pk / pk_columns / _eq)
//      with ONLY the documented `idType: "String"` provider OPTION — no patch
//  (3) the aggregate rides refine's custom/gqlQuery path against the SAME
//      native `NoteAggregate` type the library emits (no backend reshape)
//
// CJS: @refinedev/hasura's ESM build mis-imports a lodash path strict Node ESM
// rejects; `require(...)` is fine, and a real Vite/bundler frontend resolves it.
const mod = require("@refinedev/hasura");
const dataProvider = mod.default || mod;
const { GraphQLClient } = mod;
const gqlTag = require("graphql-tag");
const gql = gqlTag.gql || gqlTag.default || gqlTag;

const ENDPOINT = "http://127.0.0.1:8099/graphql";
const failures = [];
const check = (label, ok, detail = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"} ${label}${ok ? "" : " -- " + detail}`);
  if (!ok) failures.push(label);
};

// Hasura column selections are snake_case (hasura-default); the public id IS
// the sqid. `idType: "String"` makes the provider declare $id: String! so the
// opaque sqid is accepted by every pk-centric op. This is the ONE provider
// OPTION required — not a patch.
const NOTE_FIELDS = ["id", "title", "word_count", "status"];

(async () => {
  const client = new GraphQLClient(ENDPOINT);
  const dp = dataProvider(client, {
    idType: "String", // <-- sqid: id var is declared $id: String!
    namingConvention: "hasura-default",
  });

  let createdSqid;
  try {
    // getList — filter (where:{status:{_eq}}) + sort (order_by) + paging.
    // The provider also issues notes_aggregate { aggregate { count } } for total.
    const list = await dp.getList({
      resource: "notes",
      pagination: { currentPage: 1, pageSize: 10, mode: "server" },
      sorters: [{ field: "word_count", order: "desc" }],
      filters: [{ field: "status", operator: "eq", value: "published" }],
      meta: { fields: NOTE_FIELDS },
    });
    check("getList total=2 (published, via notes_aggregate count)", list.total === 2, JSON.stringify(list));
    check(
      "getList sorted DESC -> [Cee, Alpha]",
      list.data.map((d) => d.title).join(",") === "Cee,Alpha",
      JSON.stringify(list.data),
    );
    check(
      "getList ids are sqids (sq*)",
      list.data.every((d) => String(d.id).startsWith("sq")),
      JSON.stringify(list.data.map((d) => d.id)),
    );

    // getList filtering BY a sqid id -> where:{ id: { _eq: "<sqid>" } }
    const someSqid = list.data[0].id;
    const byId = await dp.getList({
      resource: "notes",
      pagination: { currentPage: 1, pageSize: 10, mode: "server" },
      filters: [{ field: "id", operator: "eq", value: someSqid }],
      meta: { fields: NOTE_FIELDS },
    });
    check(
      `getList where id _eq "${someSqid}" -> 1 row`,
      byId.total === 1 && byId.data[0].id === someSqid,
      JSON.stringify(byId),
    );

    // getOne -> notes_by_pk(id: "<sqid>")  ($id: String!)
    const one = await dp.getOne({ resource: "notes", id: someSqid, meta: { fields: NOTE_FIELDS } });
    check(`getOne by_pk(id:"${someSqid}")`, one.data?.id === someSqid, JSON.stringify(one));

    // create -> insert_notes_one(object:) ; returns entity with id = <sqid>
    const created = await dp.create({
      resource: "notes",
      variables: { title: "FromRefine", word_count: 42, status: "draft" },
      meta: { fields: NOTE_FIELDS },
    });
    createdSqid = created.data?.id;
    check("create -> FromRefine with sqid id", created.data?.title === "FromRefine" && String(createdSqid).startsWith("sq"), JSON.stringify(created));

    // update -> update_notes_by_pk(pk_columns:{id:"<sqid>"}, _set:{...})
    const upd = await dp.update({
      resource: "notes",
      id: createdSqid,
      variables: { word_count: 100 },
      meta: { fields: NOTE_FIELDS },
    });
    check("update pk_columns{id:sqid} _set{word_count:100}", upd.data?.word_count === 100 && upd.data?.id === createdSqid, JSON.stringify(upd));

    // deleteOne -> delete_notes_by_pk(id:"<sqid>")
    const del = await dp.deleteOne({ resource: "notes", id: createdSqid, meta: { fields: ["id", "title"] } });
    check("deleteOne by_pk(id:sqid)", del.data?.id === createdSqid || del.data?.title === "FromRefine", JSON.stringify(del));

    // Aggregate — rides refine's custom/gqlQuery path (provider-agnostic).
    // The document targets the SAME native NoteAggregate type the backend
    // emits with ZERO reshape. Hasura `notes_aggregate { aggregate { count
    // sum{f} avg{f} min{f} max{f} } }`.
    const aggDoc = gql`
      query NotesAgg($where: notes_bool_exp) {
        notes_aggregate(where: $where) {
          aggregate {
            count
            sum { word_count }
            avg { word_count }
            min { word_count }
            max { word_count }
          }
        }
      }
    `;
    const aggRes = await dp.custom({
      url: ENDPOINT,
      method: "post",
      meta: { gqlQuery: aggDoc, gqlVariables: { where: { status: { _eq: "published" } } } },
    });
    const agg = aggRes.data?.notes_aggregate?.aggregate;
    check("aggregate count=2 (published)", agg?.count === 2, JSON.stringify(aggRes.data));
    check("aggregate sum.word_count=30", Number(agg?.sum?.word_count) === 30, JSON.stringify(agg));
    check("aggregate avg.word_count=15", Number(agg?.avg?.word_count) === 15, JSON.stringify(agg));
    check("aggregate min.word_count=10", Number(agg?.min?.word_count) === 10, JSON.stringify(agg));
    check("aggregate max.word_count=20", Number(agg?.max?.word_count) === 20, JSON.stringify(agg));
  } catch (err) {
    check("provider call threw", false, String((err && err.stack) || err));
  }

  console.log("\n--- RESULT ---");
  console.log(
    failures.length
      ? "FAIL: " + failures.join(", ")
      : "PASS -- stock @refinedev/hasura drives the schema unmodified; sqid roundtrips via idType:String option only",
  );
  process.exit(failures.length ? 1 : 0);
})();
