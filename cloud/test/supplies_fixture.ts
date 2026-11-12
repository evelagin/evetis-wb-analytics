/**
 * Синтетические фикстуры supplies-api (M5). Ни одного реального номера поставки, штрихкода,
 * nmID или суммы: идентификаторы — заведомо выдуманные диапазоны 800000/900000/2000000000000.
 * Форма полей — как в ответах, разобранных в docs/wb_supplies_raw_design.md §3.
 */
export const SUPPLY_LIST: Array<Record<string, unknown>> = [
  {
    phone: '', supplyID: 800001, preorderID: 900001, statusID: 5, boxTypeID: 2, isBoxOnPallet: false,
    createDate: '2026-10-20T10:00:00+03:00', supplyDate: '2026-10-25T00:00:00+03:00',
    factDate: '2026-10-26T12:30:00+03:00', updatedDate: '2026-10-27T09:00:00+03:00',
  },
  {
    phone: '', supplyID: 0, preorderID: 900002, statusID: 1, boxTypeID: 2, isBoxOnPallet: false,
    createDate: '2026-11-05T10:00:00+03:00', supplyDate: null, factDate: null, updatedDate: '2026-11-05T10:00:00+03:00',
  },
];

export const SUPPLY_CARD: Record<string, unknown> = {
  warehouseID: 101, warehouseName: 'Склад-А', transitWarehouseID: 202, transitWarehouseName: 'СЦ-Б',
  actualWarehouseID: 202, actualWarehouseName: 'СЦ-Б', acceptanceCost: 0, paidAcceptanceCoefficient: 0,
  storageCoef: '1.5', deliveryCoef: '2', rejectReason: null, supplierAssignName: 'Тест',
  quantity: 30, acceptedQuantity: 28, readyForSaleQuantity: 28, unloadingQuantity: 0,
  depersonalizedQuantity: 0, canShowQuantity: true,
};

export const SUPPLY_GOODS: Array<Record<string, unknown>> = [
  {
    barcode: '2000000000011', vendorCode: 'TEST-A', nmID: 10000001, techSize: '0', color: '', tnved: '3304990000',
    needKiz: true, supplierBoxAmount: 1, quantity: 20, acceptedQuantity: 18, readyForSaleQuantity: 18, unloadingQuantity: 0,
  },
  {
    barcode: '2000000000028', vendorCode: 'TEST-B', nmID: 10000002, techSize: '0', color: '', tnved: '3304990000',
    needKiz: false, supplierBoxAmount: 1, quantity: 10, acceptedQuantity: 0, readyForSaleQuantity: 10, unloadingQuantity: 0,
  },
];
