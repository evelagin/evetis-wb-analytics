WITH u AS (SELECT DISTINCT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active AND nm_id IS NOT NULL),
wins AS (SELECT * FROM UNNEST([STRUCT('P1 31.08-13.09' AS wn, DATE '2026-08-31' AS d0, DATE '2026-09-13' AS d1), ('P2 17-30.08','2026-08-17','2026-08-30'), ('P3 27.07-23.08','2026-07-27','2026-08-23'), ('30d 18.08-16.09','2026-08-18','2026-09-16'), ('since 13.04','2026-04-13','2026-09-16')])),
a AS (SELECT wn, ROUND(SUM(stats_spend_rub),2) attr_all, ROUND(SUM(IF(nm_id IN (SELECT nm_id FROM u), stats_spend_rub, 0)),2) attr_univ,
        ROUND(SUM(IF(nm_id IS NULL OR nm_id NOT IN (SELECT nm_id FROM u), stats_spend_rub, 0)),2) attr_outside, ARRAY_AGG(DISTINCT advert_id) ids
      FROM wins JOIN `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY` x ON x.date BETWEEN d0 AND d1 GROUP BY wn),
c AS (SELECT wn, advert_id, SUM(actual_spend_rub) s FROM wins JOIN `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY` x ON x.date BETWEEN d0 AND d1 GROUP BY wn, advert_id)
SELECT wins.wn, a.attr_all, a.attr_univ, a.attr_outside, ROUND(SUM(c.s),2) billing,
  ROUND(SUM(IF(c.advert_id NOT IN UNNEST(a.ids), c.s, 0)),2) billing_campaigns_without_stats,
  COUNTIF(c.advert_id NOT IN UNNEST(a.ids)) n_camp_no_stats, ROUND(a.attr_all - SUM(c.s),2) attr_minus_billing
FROM wins LEFT JOIN a USING (wn) LEFT JOIN c USING (wn) GROUP BY wins.wn, a.attr_all, a.attr_univ, a.attr_outside, wins.d0 ORDER BY wins.d0
