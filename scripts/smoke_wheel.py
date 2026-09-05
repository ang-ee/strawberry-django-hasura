"""Exercise the installed wheel outside the source checkout."""

from django.conf import settings

settings.configure(
    INSTALLED_APPS=[],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    },
    USE_TZ=True,
)

import django  # noqa: E402

django.setup()

import strawberry  # noqa: E402
import strawberry_django  # noqa: E402
from django.db import connection, models  # noqa: E402
from strawberry import auto  # noqa: E402

from strawberry_django_hasura import hasura_resource  # noqa: E402


class Item(models.Model):
    amount = models.IntegerField()

    class Meta:
        app_label = "smoke"


@strawberry_django.type(Item)
class ItemNode:
    id: auto
    amount: auto


resource = hasura_resource(
    ItemNode,
    model=Item,
    name="items",
    filterable=["id", "amount"],
    sortable=["id"],
    aggregatable=["amount"],
    groupable=["amount"],
    get_queryset=lambda info: Item.objects.all(),
    write_backend=None,
    insert=False,
    update=False,
    delete=False,
)
schema = strawberry.Schema(query=resource.query, types=resource.types)
with connection.schema_editor() as editor:
    editor.create_model(Item)
Item.objects.create(amount=3)
result = schema.execute_sync("""
    {
      items { amount }
      items_aggregate { aggregate { count sum { __typename amount } } }
      items_groups(group_by: [{field: AMOUNT}]) {
        aggregate { count }
      }
      items_groups_count(group_by: [{field: AMOUNT}])
    }
""")
assert result.errors is None, result.errors
assert result.data["items"] == [{"amount": 3}]
assert result.data["items_aggregate"]["aggregate"]["sum"]["amount"] == "3"
assert result.data["items_groups"] == [{"aggregate": {"count": 1}}]
assert result.data["items_groups_count"] == 1
print("Installed wheel: list, aggregate, grouping and group count passed")
