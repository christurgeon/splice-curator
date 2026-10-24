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
        tags { label taxonomy { name } }
        files { asset_file_type_slug url }
      }
      ... on IAssetChild {
        parents(filter: {asset_type_slug: pack}) {
          items { ... on PackAsset { uuid name main_genre provider { name } } }
        }
      }
      ... on SampleAsset { bpm key chord_type duration instrument asset_category_slug }
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
      asset_prices { amount currency }
      files(includeSource: true) { asset_file_type_slug url }
    }
  }
}""")

PURCHASE = Template("""mutation PurchaseAsset {
  purchaseAsset(uuid: "$uuid") { ... on IAsset { uuid licensed } }
}""")

PACK_SEARCH = Template("""query PackSearch {
  assetsSearch(
    filter: {asset_type_slug: pack$filter}
    pagination: {page: $page, limit: $limit}
    sort: {sort: popularity, order: DESC}
  ) {
    response_metadata { records }
    pagination_metadata { currentPage totalPages }
    items {
      ... on PackAsset {
        uuid name main_genre permalink_slug published_at
        provider { name }
        child_asset_counts { type count }
        tags { label }
      }
    }
  }
}""")

PACKS_CHART = Template("""query PacksChart {
  legacyPacksChart(searchParams: {perPage: $per_page$provider}) {
    uuid name permalink_slug current_global_chart_pos last_global_chart_pos
    provider { uuid name permalink_slug }
    child_asset_counts { count type }
  }
}""")

LABELS_CHART = Template("""query LabelsChart {
  wwwPremiumProviders(sort: "chart", per_page: $per_page) {
    uuid legacy_uuid name permalink_slug
    current_provider_chart_position last_provider_chart_position
    pack_count tags_array
  }
}""")
