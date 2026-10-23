"""GraphQL documents, shaped after the verified queries in the design spec.

GraphQL introspection is disabled, so variable type names are unknown. To
avoid guessing them, values are inlined as literals with ``string.Template``
(``$name`` placeholders). Callers must pass only validated values; see
``client.py``.
"""

from string import Template

ACCOUNT = "query Account { user { username } subscription { credits state } }"

SEARCH = Template("""query SamplesSearch {
  assetsSearch(
    filter: {legacy: true, published: true, asset_type_slug: sample$filter}
    pagination: {page: $page, limit: $limit}
    sort: {sort: $sort, order: DESC}
    legacy: {use: true}
  ) {
    response_metadata { records }
    pagination_metadata { currentPage totalPages }
    items {
      ... on IAsset {
        uuid
        name
        licensed
        asset_category_slug
        tags { label taxonomy { name } }
        files { asset_file_type_slug url }
      }
      ... on IAssetChild {
        parents(filter: {asset_type_slug: pack}) {
          items { ... on PackAsset { name main_genre provider { name } } }
        }
      }
      ... on SampleAsset { bpm key chord_type duration instrument }
      ... on ILegacyAsset { catalog_uuid }
    }
  }
}""")

ASSET_FILES = Template("""query AssetFiles {
  asset(uuid: "$uuid") {
    __typename
    ... on SampleAsset {
      name
      licensed
      files(includeSource: true) { asset_file_type_slug url }
    }
  }
}""")

PURCHASE = Template("""mutation PurchaseAsset {
  purchaseAsset(uuid: "$uuid") { ... on IAsset { uuid licensed } }
}""")
