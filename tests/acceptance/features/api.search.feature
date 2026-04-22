@acceptance @api @mvp @xfail_contract
Feature: Search API
  The API returns stable projected-path selectors that can be reused directly by pin and release.

  Background:
    Given an archive containing deterministic fixture collections
    And collection "docs" contains file "/tax/2022/invoice-123.pdf"
    And collection "photos-2024" contains directory "/albums/japan/"

  Scenario: Search returns file and collection selectors
    When the client gets "/v1/search?q=invoice&limit=25"
    Then the response status is 200
    And the response query is "invoice"
    And the response contains at least one file result
    And each file result contains a projected-path selector
    And each file result contains current hot availability
    And each file result contains available copies if archived

  Scenario: Search selectors are directly reusable
    When the client gets "/v1/search?q=japan&limit=25"
    Then the response status is 200
    And every returned target is valid input for pin
    And every returned target is valid input for release

  Scenario: Search honors limit
    When the client gets "/v1/search?q=a&limit=1"
    Then the response status is 200
    And the response contains at most 1 result

  Scenario: Search is case-insensitive substring match
    When the client gets "/v1/search?q=INVOICE&limit=25"
    Then the response status is 200
    And the response contains target "docs/tax/2022/invoice-123.pdf"
