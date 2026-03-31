system_prompt = """
You are a financial analysis assistant that generates structured fundamental stock reports in JSON format 

Your job:
- Analyze the given company's data (industry, employees, market cap, financial metrics, quarterly data, etc.) and produce a clean, consistent, and LLM-friendly JSON output compatible with OpenAI’s `financial_fundamentals` schema.
- You must ensure **no missing required fields**, including every key mentioned in the schema (for example, "tax_effect_of_unusual_items" must be included even if null).
- All numerical data should be in proper numeric types (not strings).
- If data is missing, include `null` instead of omitting the field.
- Always reference data as of the **current date**.
- Maintain schema alignment for sub-objects like `quarterly_financials`, `balance_sheet`, `income_statement`, and `cash_flow_statement`.

Output Rules:
- The root JSON object should have these main sections:
  1. `company_overview`
  2. `financial_metrics`
  3. `quarterly_financials`
  4. `balance_sheet`
  5. `income_statement`
  6. `cash_flow_statement`
- Dates must be in `YYYY-MM-DD` format.
- All monetary values must be in crores (₹ Cr).
- Use current date as `report_generated_at`.

Purpose:
This JSON will be used as a structured input for OpenAI models for further reasoning, summarization, and analytics.
"""

output_schema = {
    "format": {
      "type": "json_schema",
      "name": "financial_fundamentals",
      "strict": True,
      "schema": {
        "type": "object",
        "properties": {
          "industry": {
            "type": "string",
            "description": "Industry sector to which the company belongs.",
            "minLength": 1
          },
          "sector_details": {
            "type": "object",
            "properties": {
              "category": {
                "type": "string",
                "description": "Specific sector or industry category.",
                "minLength": 1
              }
            },
            "required": [
              "category"
            ],
            "additionalProperties": False
          },
          "company_overview": {
            "type": "object",
            "properties": {
              "employees": {
                "type": "integer",
                "description": "Number of employees in the company."
              },
              "market_cap_cr": {
                "type": "number",
                "description": "Company market capitalization in crores."
              },
              "data_age_note": {
                "type": "string",
                "description": "Annotation about the age or freshness of data.",
                "minLength": 1
              }
            },
            "required": [
              "employees",
              "market_cap_cr",
              "data_age_note"
            ],
            "additionalProperties": False
          },
          "financial_metrics": {
            "type": "object",
            "properties": {
              "source": {
                "type": "string"
              },
              "data_as_of": {
                "type": "string",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
              },
              "revenue_and_profitability": {
                "type": "object",
                "properties": {
                  "revenue_cr": {
                    "type": "number"
                  },
                  "revenue_change_pct": {
                    "type": "number"
                  },
                  "net_income_cr": {
                    "type": "number"
                  },
                  "net_income_change_pct": {
                    "type": "number"
                  },
                  "ebitda_cr": {
                    "type": "number"
                  },
                  "operating_income_cr": {
                    "type": "number"
                  }
                },
                "required": [
                  "revenue_cr",
                  "revenue_change_pct",
                  "net_income_cr",
                  "net_income_change_pct",
                  "ebitda_cr",
                  "operating_income_cr"
                ],
                "additionalProperties": False
              },
              "margin_analysis": {
                "type": "object",
                "properties": {
                  "gross_margin_pct": {
                    "type": "number"
                  },
                  "operating_margin_pct": {
                    "type": "number"
                  },
                  "ebitda_margin_pct": {
                    "type": "number"
                  },
                  "net_margin_pct": {
                    "type": "number"
                  }
                },
                "required": [
                  "gross_margin_pct",
                  "operating_margin_pct",
                  "ebitda_margin_pct",
                  "net_margin_pct"
                ],
                "additionalProperties": False
              },
              "balance_sheet": {
                "type": "object",
                "properties": {
                  "total_assets_cr": {
                    "type": "number"
                  },
                  "shareholders_equity_cr": {
                    "type": "number"
                  },
                  "current_ratio": {
                    "type": "number"
                  },
                  "debt_to_equity": {
                    "type": "number"
                  }
                },
                "required": [
                  "total_assets_cr",
                  "shareholders_equity_cr",
                  "current_ratio",
                  "debt_to_equity"
                ],
                "additionalProperties": False
              },
              "cash_flow": {
                "type": "object",
                "properties": {},
                "additionalProperties": False
              }
            },
            "required": [
              "source",
              "data_as_of",
              "revenue_and_profitability",
              "margin_analysis",
              "balance_sheet",
              "cash_flow"
            ],
            "additionalProperties": False
          },
          "quarterly_financials": {
            "type": "object",
            "properties": {
              "quarters": {
                "type": "array",
                "items": {
                  "type": "object",
                  "properties": {
                    "date": {
                      "type": "string",
                      "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
                    },
                    "tax_effect_of_unusual_items": {
                      "type": "number"
                    },
                    "tax_rate_for_calcs": {
                      "type": "number"
                    },
                    "normalized_ebitda": {
                      "type": "number"
                    },
                    "net_income_from_continuing_ops": {
                      "type": "number"
                    },
                    "reconciled_depreciation": {
                      "type": "number"
                    },
                    "reconciled_cost_of_revenue": {
                      "type": "number"
                    },
                    "ebitda": {
                      "type": "number"
                    },
                    "ebit": {
                      "type": "number"
                    },
                    "net_interest": {
                      "type": "number"
                    }
                  },
                  "required": [
                    "date",
                    "tax_effect_of_unusual_items",
                    "tax_rate_for_calcs",
                    "normalized_ebitda",
                    "net_income_from_continuing_ops",
                    "reconciled_depreciation",
                    "reconciled_cost_of_revenue",
                    "ebitda",
                    "ebit",
                    "net_interest"
                  ],
                  "additionalProperties": False
                }
              }
            },
            "required": [
              "quarters"
            ],
            "additionalProperties": False
          },
          "meta": {
            "type": "object",
            "properties": {
              "tip": {
                "type": "string",
                "minLength": 1
              }
            },
            "required": [
              "tip"
            ],
            "additionalProperties": False
          }
        },
        "required": [
          "industry",
          "sector_details",
          "company_overview",
          "financial_metrics",
          "quarterly_financials",
          "meta"
        ],
        "additionalProperties": False
      }
    }
  }