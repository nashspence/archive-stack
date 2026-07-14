@acceptance @api @mvp
Feature: Search API
  The API returns a paged file inventory with stable projected-path selectors that can be reused directly by fetch creation and hot eviction.

  Background:
    Given an archive containing deterministic fixture collections
    And collection "docs" contains file "/tax/2022/invoice-123.pdf"
    And collection "photos-2024" contains directory "/albums/japan/"

  Scenario: Search returns paged file selectors
    When the client gets "/v1/search?q=invoice&page=1&per_page=25"
    Then the response status is 200
    And the response query is "invoice"
    And the response contains "query", "collection", "hot", "disc_coverage", "page", "per_page", "total", "pages", "sort", "order", and "files"
    And the response contains at least one file result
    And each file result contains a projected-path selector
    And each file result contains current hot availability
    And each file entry contains "target", "collection", "path", "bytes", "sha256", "hot", and "disc_coverage"

  Scenario: Search selectors are directly reusable
    When the client gets "/v1/search?q=japan&page=1&per_page=25"
    Then the response status is 200
    And every returned target is valid input for fetch creation
    And every returned target is valid input for hot eviction

  Scenario: Search is paginated
    When the client gets "/v1/search?q=a&page=1&per_page=1"
    Then the response status is 200
    And the response contains at most 1 result

  Scenario: Search is case-insensitive substring match
    When the client gets "/v1/search?q=INVOICE&page=1&per_page=25"
    Then the response status is 200
    And the response contains target "docs/tax/2022/invoice-123.pdf"

  Scenario: Search can filter by collection and disc coverage
    When the client gets "/v1/search?collection=docs&disc_coverage=true&sort=bytes&order=desc"
    Then the response status is 200
    And each file entry contains "target", "collection", "path", "bytes", "sha256", "hot", and "disc_coverage"
