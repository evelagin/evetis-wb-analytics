-- ОТКАТ hotfix unidentified_return_v1: тело V_UNITKA_LOGISTICS_RATES в production ДО изменения 2026-11-06 (git main 01eb0738, live = Git, проверено).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LOGISTICS_RATES` AS
WITH w AS (
  SELECT DATE_SUB(last_closed_date, INTERVAL 29 DAY) AS d1, last_closed_date AS d2
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`
),
b AS (
  SELECT srid, SAFE_CAST(wb_nm_id AS INT64) AS nm, supplier_oper_name AS son,
         SAFE_CAST(logistics_amount AS NUMERIC) AS amt, JSON_VALUE(raw_json, '$.bonusTypeName') AS bonus
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`, w
  WHERE _rr_date BETWEEN w.d1 AND w.d2 AND wb_nm_id IS NOT NULL AND srid IS NOT NULL
),
-- Возврат брака продавцу (инцидент 27.10.2026, unitka-engine-prod-qr5xr): покупатель вернул брак, WB везёт единицу со
-- склада в ПВЗ продавца отдельным srid — одно плечо «Доставка»/«Логистика» с bonusTypeName «Возврат брака (К продавцу)»,
-- без «Продажи» и «Возврата» (они — у исходного srid покупателя). Это не отправка покупателю: srid не входит ни в
-- sales, ни в refusals, ни в хвост, а его плечо — ни в прямую, ни в обратную ставку (seller_return / seller_return_sum;
-- деньги — в Store P&L по факту финотчёта, когорта даты заказа × nm). srid с таким плечом другой формы (есть «Продажа»,
-- «Возврат» или второе плечо) не попадает НИ в один класс — инвариант Engine отклоняет его (fail-closed).
sr AS (SELECT DISTINCT srid FROM b WHERE son IN ('Логистика', 'Доставка') AND bonus = 'Возврат брака (К продавцу)'),
sale AS (SELECT COUNT(DISTINCT srid) AS n FROM b WHERE son = 'Продажа' AND srid NOT IN (SELECT srid FROM sr)),
l AS (
  SELECT srid, nm, son, amt,
         ROW_NUMBER() OVER (PARTITION BY srid ORDER BY amt DESC) AS rn,
         COUNT(*) OVER (PARTITION BY srid) AS legs
  FROM b WHERE son IN ('Логистика', 'Доставка')
),
p AS (
  SELECT srid, ANY_VALUE(nm) AS nm, ANY_VALUE(son) AS son,
         SUM(IF(rn = 1, amt, 0)) AS fwd, SUM(IF(rn > 1, amt, 0)) AS rev, MAX(legs) AS legs
  FROM l GROUP BY srid
),
-- Хвост доказанного возврата: единственное плечо srid в окне — ОБРАТНОЕ (прямое ушло за начало окна вместе с
-- «Продажей»). Чтобы граница окна не превращала обратное плечо в прямое, хвост не входит в популяцию прямых
-- (числитель и знаменатель direct_rate), а его плечо идёт в пул обратных (reverse_sum, знаменатель reverse_rate =
-- refusals + хвосты) — так же, как это плечо учитывалось, пока продажа была в окне. У SKU return_tail = число хвостов
-- (Engine считает порог своей ставки по прямым: shipments − return_tail). Без хвостов (обычное состояние)
-- результат совпадает с прежним определением побайтно. shipments и refusals не меняют смысла (инвариант V1).
px AS (SELECT p.*, srid IN (SELECT srid FROM sr) AS srx FROM p),
pt AS (
  SELECT px.*, (NOT srx AND legs = 1 AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа')
                            AND srid IN (SELECT srid FROM b WHERE son = 'Возврат')) AS tail,
               (srx AND legs = 1 AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа')
                    AND srid NOT IN (SELECT srid FROM b WHERE son = 'Возврат')) AS sret
  FROM px
),
a AS (
  SELECT nm, COUNT(*) AS n, COUNTIF(tail) AS nt, COUNTIF(sret) AS ns, SUM(IF(tail OR sret, 0, fwd)) AS fwd,
         COUNTIF(legs >= 2 AND NOT srx) AS ref,
         SUM(rev) + SUM(IF(tail, fwd, 0)) AS rev, SUM(IF(sret, fwd, 0)) AS srsum,
         COUNTIF(son = 'Логистика') AS nlog, COUNTIF(son = 'Доставка') AS ndlv,
         SUM(IF(son = 'Доставка' AND NOT tail AND NOT sret, fwd, 0)) AS dlvfwd
  FROM pt GROUP BY nm
),
-- Продажа с последующим возвратом (инцидент 20.10.2026, unitka-engine-prod-qs2p4): у srid есть «Продажа»
-- и два плеча логистики, поэтому он одновременно в sales и в refusals. Пересечение считается независимо —
-- по уникальным srid, входящим в ОБЕ группы, на том же срезе b и в том же окне; ставки не меняются.
ov AS (
  SELECT COUNT(*) AS n
  FROM p
  WHERE legs >= 2 AND srid IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid NOT IN (SELECT srid FROM sr)
),
-- Хвост доказанного возврата (follow-up инцидента 20.10.2026): «Продажа» srid ушла за начало окна, а «Возврат» и его
-- плечо остались в окне. Такой srid в shipments (одно плечо), но ни в sales, ни в refusals. Считается независимо:
-- уникальные srid с ровно одним плечом в окне, БЕЗ «Продажи» и С «Возвратом» на том же срезе b. Одиночное плечо без
-- «Возврата» (только «Возмещение…» или ничего) сюда не входит и по-прежнему отклоняется. Классификация плеча — pt/a.
rt AS (
  SELECT COUNT(*) AS n
  FROM p
  WHERE legs = 1
    AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа')
    AND srid IN (SELECT srid FROM b WHERE son = 'Возврат')
    AND srid NOT IN (SELECT srid FROM sr)
),
-- Возврат брака продавцу — независимый счётчик магазина (см. sr): одно плечо с этим bonusTypeName, без «Продажи» и
-- без «Возврата» в окне. Если позже по srid придёт «Продажа»/«Возврат»/второе плечо — он выпадет отсюда и не попадёт
-- ни в один класс (инвариант отклонит), а не будет учтён дважды.
sx AS (
  SELECT COUNT(*) AS n
  FROM p
  WHERE legs = 1
    AND srid IN (SELECT srid FROM sr)
    AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа')
    AND srid NOT IN (SELECT srid FROM b WHERE son = 'Возврат')
)
SELECT nm AS nm_id, n AS shipments, ROUND(SAFE_DIVIDE(fwd, NULLIF(n - nt - ns, 0)), 4) AS direct_rate,
       ref AS refusals, ROUND(SAFE_DIVIDE(rev, NULLIF(ref + nt, 0)), 4) AS reverse_rate,
       ROUND(fwd, 2) AS forward_sum, ROUND(rev, 2) AS reverse_sum,
       nlog AS n_logistics, ndlv AS n_delivery, ROUND(dlvfwd, 2) AS delivery_component_sum,
       CAST(NULL AS INT64) AS sales, (SELECT d1 FROM w) AS window_from, (SELECT d2 FROM w) AS window_to,
       CAST(NULL AS INT64) AS sale_refusal_overlap, nt AS return_tail, ns AS seller_return, ROUND(srsum, 2) AS seller_return_sum
FROM a
UNION ALL
SELECT 0, SUM(n), ROUND(SAFE_DIVIDE(SUM(fwd), NULLIF(SUM(n) - SUM(nt) - SUM(ns), 0)), 4),
       SUM(ref), ROUND(SAFE_DIVIDE(SUM(rev), NULLIF(SUM(ref) + SUM(nt), 0)), 4),
       ROUND(SUM(fwd), 2), ROUND(SUM(rev), 2),
       SUM(nlog), SUM(ndlv), ROUND(SUM(dlvfwd), 2),
       (SELECT n FROM sale), (SELECT d1 FROM w), (SELECT d2 FROM w),
       (SELECT n FROM ov), (SELECT n FROM rt), (SELECT n FROM sx), ROUND(SUM(srsum), 2)
FROM a;
