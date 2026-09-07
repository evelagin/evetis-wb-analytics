-- ============================================================================
-- PR-2 — сверка развёрнутых объектов с репозиторием.
-- Сравнивается SHA-256 тела объекта в production с хешем, зафиксированным
-- в sql/pricing/pr2_wb_forward_economics.sql на момент коммита.
-- Текстовое равенство: BigQuery хранит тело дословно, нормализации нет.
-- Ожидаемый результат: match = TRUE для всех строк.
-- ============================================================================
WITH expected AS (
  SELECT * FROM UNNEST([
    STRUCT('V_WB_TARIFFS_CURRENT' AS obj, '99ac20833e5185766b3e6a58499e0e0a233adb42f5031bef86794b7e1aec722b' AS sha256, 618 AS len),
    STRUCT('V_WB_SKU_COST_INPUTS' AS obj, '17f2445b1f08f3edd5789e5ef060914b99715c3ee905a6fceb00f6f73268733e' AS sha256, 5462 AS len),
    STRUCT('V_WB_SKU_FORWARD_ECONOMICS_CURRENT' AS obj, 'd1de676090e8585ec02379b649710d1383d5728c6b29a22160f18804d50c524c' AS sha256, 5264 AS len),
    STRUCT('TVF_WB_FORWARD_ECONOMICS' AS obj, '0423e1953bb948de3cdaf33daad0f4d135a3bf0a008a40b1f810c6c77dfa15f3' AS sha256, 1989 AS len),
    STRUCT('V_WB_PRICING_ECONOMICS_HEALTH' AS obj, 'a64bdf2e477f257df125411a25870547eb606670317d0c95ba4a91970542f938' AS sha256, 2340 AS len)
  ])
),
deployed AS (
  SELECT table_name AS obj, TO_HEX(SHA256(view_definition)) AS sha256, LENGTH(view_definition) AS len
  FROM `project-fa311fc0-4d87-4781-986.wb_raw`.INFORMATION_SCHEMA.VIEWS
  WHERE table_name = 'V_WB_TARIFFS_CURRENT'
  UNION ALL
  SELECT table_name, TO_HEX(SHA256(view_definition)), LENGTH(view_definition)
  FROM `project-fa311fc0-4d87-4781-986.wb_mart`.INFORMATION_SCHEMA.VIEWS
  WHERE table_name LIKE 'V_WB_%'
  UNION ALL
  SELECT routine_name, TO_HEX(SHA256(routine_definition)), LENGTH(routine_definition)
  FROM `project-fa311fc0-4d87-4781-986.wb_mart`.INFORMATION_SCHEMA.ROUTINES
  WHERE routine_name = 'TVF_WB_FORWARD_ECONOMICS'
)
SELECT e.obj, e.sha256 AS expected_sha, d.sha256 AS deployed_sha, e.len AS expected_len, d.len AS deployed_len,
       (e.sha256 = d.sha256) AS match
FROM expected e LEFT JOIN deployed d USING (obj)
ORDER BY e.obj;
