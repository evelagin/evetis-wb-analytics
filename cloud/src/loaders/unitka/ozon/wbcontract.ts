/**
 * CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT — статический визуальный контракт Unitka.
 *
 * Снят с ЖИВОЙ секции текущего поколения `WB_Юнит_2025 / Сентябрь 2026` (Gate 5D, §3 Gate 5E).
 * Это единственный источник оформления для Ozon: ширины колонок, шрифты, выравнивания,
 * рамки, числовые форматы и статические заливки по КАЖДОЙ роли и КАЖДОЙ роли строки.
 *
 * Контракт описывает текущее поколение и НЕ утверждает, что таким же оформлением обладают
 * исторические секции WB. Исторический WB не трогаем: он остаётся как есть, а контракт
 * фиксирует то, как выглядит месяц, созданный сегодняшним движком.
 *
 * Файл сгенерирован замером живого листа и правится только повторным замером,
 * а не редактированием значений вручную.
 *
 * VISUAL PARITY (2026-09-24): первый замер снял у рамок только СТИЛЬ, без цвета, и Ozon рисовал
 * чёрным всё, что у WB серое: контур заголовка SKU и строки итога (#5f6368) и сетку шапки
 * (#9aa0a6). Повторный замер той же секции добавил `borderColors` — только для сторон, чей цвет
 * отличается от чёрного (#000000 / тема TEXT). Стили рамок при этом совпали с замером.
 */
export interface CellFormatSpec {
  fontFamily?: string; fontSize?: number; bold?: boolean; italic?: boolean;
  fg?: string; bg?: string; ha?: string; va?: string; wrap?: string;
  borders?: Partial<Record<'top' | 'bottom' | 'left' | 'right', string>>;
  /** Цвет стороны рамки, если он не чёрный. Сторона без записи — чёрная, как и раньше. */
  borderColors?: Partial<Record<'top' | 'bottom' | 'left' | 'right', string>>;
  numberFormat?: { type?: string; pattern?: string };
}
export interface PresentationContract {
  source: string;
  widths: { summary: Record<string, number>; block: Record<string, number> };
  rows: Record<'title' | 'header' | 'day' | 'mtd',
               { summary: Record<string, CellFormatSpec>; block: Record<string, CellFormatSpec> }>;
}

export const CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT: PresentationContract =
{
  "source": "WB_Юнит_2025 / Сентябрь 2026 (текущее поколение)",
  "widths": {
    "summary": {
      "WEEKDAY": 73,
      "DATE": 73,
      "MANUAL_EXTERNAL": 92,
      "IMPRESSIONS": 102,
      "CLICKS": 82,
      "ORDERS": 85,
      "CART": 85,
      "CANCELLATIONS": 85,
      "TOTAL_PROFIT": 104,
      "INTERNAL_ADS": 100
    },
    "block": {
      "DATE": 84,
      "MANUAL_EXTERNAL": 84,
      "IMPRESSIONS": 84,
      "CLICKS": 84,
      "ORDERS": 84,
      "CART": 84,
      "CANCELLATIONS": 84,
      "STOCK": 84,
      "TURNOVER": 84,
      "UNIT_PROFIT": 84,
      "TOTAL_PROFIT": 104,
      "INTERNAL_ADS": 100,
      "EXTERNAL_ADS": 84,
      "DRR": 84,
      "SELLER_PRICE": 84,
      "DISCOUNT": 84,
      "BUYER_PRICE": 84,
      "COMMISSION": 84,
      "NET_AFTER_COMMISSION": 84,
      "LOGISTICS": 84,
      "STORAGE": 84,
      "TAX_RESERVE": 84,
      "FINAL_UNIT_PROFIT": 84,
      "WEEKDAY": 56
    }
  },
  "rows": {
    "title": {
      "summary": {
        "WEEKDAY": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#ffffff",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_THICK",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_THICK",
            "right": "SOLID_THICK"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "DATE": {
          "bg": "#ffffff"
        },
        "MANUAL_EXTERNAL": {
          "bg": "#ffffff"
        },
        "IMPRESSIONS": {
          "bg": "#ffffff"
        },
        "CLICKS": {
          "bg": "#ffffff"
        },
        "ORDERS": {
          "bg": "#ffffff"
        },
        "CART": {
          "bg": "#ffffff"
        },
        "CANCELLATIONS": {
          "bg": "#ffffff"
        },
        "TOTAL_PROFIT": {
          "bg": "#ffffff"
        },
        "INTERNAL_ADS": {
          "bg": "#ffffff"
        }
      },
      "block": {
        "DATE": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368",
            "left": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "MANUAL_EXTERNAL": {
          "bg": "#ffffff"
        },
        "IMPRESSIONS": {
          "bg": "#ffffff"
        },
        "CLICKS": {
          "bg": "#ffffff"
        },
        "ORDERS": {
          "bg": "#ffffff"
        },
        "CART": {
          "bg": "#ffffff"
        },
        "CANCELLATIONS": {
          "bg": "#ffffff"
        },
        "STOCK": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "TURNOVER": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "UNIT_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "TOTAL_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "INTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "EXTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "DRR": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "SELLER_PRICE": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "DISCOUNT": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "BUYER_PRICE": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "COMMISSION": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "NET_AFTER_COMMISSION": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "LOGISTICS": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "STORAGE": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "TAX_RESERVE": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "FINAL_UNIT_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        },
        "WEEKDAY": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "italic": true,
          "bg": "#dce8f2",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368",
            "right": "#5f6368"
          },
          "numberFormat": {
            "type": "TEXT"
          }
        }
      }
    },
    "header": {
      "summary": {
        "WEEKDAY": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#ffffff",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_THICK",
            "right": "SOLID"
          }
        },
        "DATE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#d9d9d9",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID_MEDIUM"
          }
        },
        "MANUAL_EXTERNAL": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#ffe599",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID"
          }
        },
        "IMPRESSIONS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID"
          }
        },
        "CLICKS": {
          "fontFamily": "Calibri",
          "fontSize": 11,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID"
          }
        },
        "ORDERS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID"
          }
        },
        "CART": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID"
          }
        },
        "CANCELLATIONS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID"
          }
        },
        "TOTAL_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID"
          }
        },
        "INTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID"
          }
        }
      },
      "block": {
        "DATE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#d9d9d9",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "MANUAL_EXTERNAL": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#ffe599",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "IMPRESSIONS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "CLICKS": {
          "fontFamily": "Calibri",
          "fontSize": 11,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "ORDERS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "CART": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "CANCELLATIONS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "STOCK": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "TURNOVER": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "UNIT_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "TOTAL_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "INTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "EXTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "DRR": {
          "fontFamily": "Calibri",
          "fontSize": 20,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "SELLER_PRICE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "DISCOUNT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#ffe599",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "BUYER_PRICE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "COMMISSION": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#cfe2f3",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "NET_AFTER_COMMISSION": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "LOGISTICS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#cfe2f3",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "STORAGE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#b7e1cd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "TAX_RESERVE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "FINAL_UNIT_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#efefef",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        },
        "WEEKDAY": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "fg": "#ffffff",
          "bg": "#d9d9d9",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "borderColors": {
            "bottom": "#9aa0a6",
            "left": "#9aa0a6",
            "right": "#9aa0a6"
          }
        }
      }
    },
    "day": {
      "summary": {
        "WEEKDAY": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "left": "SOLID_THICK",
            "right": "SOLID"
          }
        },
        "DATE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID_MEDIUM"
          },
          "numberFormat": {
            "type": "DATE",
            "pattern": "dd.MM.yy"
          }
        },
        "MANUAL_EXTERNAL": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#fffbe8",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID_MEDIUM",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "IMPRESSIONS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CLICKS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "ORDERS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CART": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CANCELLATIONS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "TOTAL_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#ffffff",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "INTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        }
      },
      "block": {
        "DATE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "left": "#5f6368"
          },
          "numberFormat": {
            "type": "DATE",
            "pattern": "dd.MM.yy"
          }
        },
        "MANUAL_EXTERNAL": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#fffbe8",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID_MEDIUM",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "IMPRESSIONS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CLICKS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "ORDERS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CART": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CANCELLATIONS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "STOCK": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "TURNOVER": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "0"
          }
        },
        "UNIT_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "italic": true,
          "bg": "#ffffff",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "TOTAL_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#ffffff",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "INTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "EXTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "DRR": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#ffffff",
          "ha": "CENTER",
          "va": "MIDDLE",
          "wrap": "WRAP",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "PERCENT",
            "pattern": "0.0%"
          }
        },
        "SELLER_PRICE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#fffbe8",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "DISCOUNT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#fffbe8",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "BUYER_PRICE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "COMMISSION": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f3f8fd",
          "ha": "CENTER",
          "va": "MIDDLE",
          "numberFormat": {
            "type": "PERCENT",
            "pattern": "0.0%"
          }
        },
        "NET_AFTER_COMMISSION": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "LOGISTICS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f3f8fd",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "STORAGE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#f1f8f4",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "TAX_RESERVE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "FINAL_UNIT_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID",
            "bottom": "SOLID",
            "left": "SOLID",
            "right": "SOLID_MEDIUM"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "WEEKDAY": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "right": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "General"
          }
        }
      }
    },
    "mtd": {
      "summary": {
        "WEEKDAY": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bg": "#ffffff",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID_THICK",
            "right": "SOLID"
          }
        },
        "DATE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID",
            "right": "SOLID_MEDIUM"
          }
        },
        "MANUAL_EXTERNAL": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID_MEDIUM",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "IMPRESSIONS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CLICKS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "ORDERS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CART": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CANCELLATIONS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "TOTAL_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "INTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_THICK",
            "left": "SOLID",
            "right": "SOLID"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        }
      },
      "block": {
        "DATE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368",
            "left": "#5f6368"
          }
        },
        "MANUAL_EXTERNAL": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "IMPRESSIONS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CLICKS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "ORDERS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CART": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "CANCELLATIONS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "STOCK": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "TURNOVER": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "UNIT_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "TOTAL_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "INTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "EXTERNAL_ADS": {
          "fontFamily": "Calibri",
          "fontSize": 16,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "DRR": {
          "fontFamily": "Calibri",
          "fontSize": 14,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "PERCENT",
            "pattern": "0.0%"
          }
        },
        "SELLER_PRICE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "DISCOUNT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0"
          }
        },
        "BUYER_PRICE": {
          "fontFamily": "Calibri",
          "fontSize": 11,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "COMMISSION": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "PERCENT",
            "pattern": "0.0%"
          }
        },
        "NET_AFTER_COMMISSION": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "LOGISTICS": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID_MEDIUM",
            "right": "SOLID"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "STORAGE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "left": "SOLID",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "TAX_RESERVE": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "FINAL_UNIT_PROFIT": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "RIGHT",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368"
          },
          "numberFormat": {
            "type": "NUMBER",
            "pattern": "#,##0\\ \"₽\""
          }
        },
        "WEEKDAY": {
          "fontFamily": "Calibri",
          "fontSize": 12,
          "bold": true,
          "bg": "#e8eaed",
          "ha": "CENTER",
          "va": "MIDDLE",
          "borders": {
            "top": "SOLID_MEDIUM",
            "bottom": "SOLID_MEDIUM",
            "right": "SOLID_MEDIUM"
          },
          "borderColors": {
            "top": "#5f6368",
            "bottom": "#5f6368",
            "right": "#5f6368"
          }
        }
      }
    }
  }
};
