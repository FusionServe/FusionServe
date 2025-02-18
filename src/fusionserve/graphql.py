import strawberry
from strawberry.fastapi import GraphQLRouter


@strawberry.type
class Query:
    @strawberry.field
    def hello(self) -> str:
        return "Hello World"


schema = strawberry.Schema(Query)

router = GraphQLRouter(schema, allow_queries_via_get=False, keep_alive=True, debug=True)
