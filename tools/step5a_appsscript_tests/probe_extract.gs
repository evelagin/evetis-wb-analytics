function wbAdsLast7Range_() {
  var y = new Date(); y.setDate(y.getDate() - 1);
  var f = new Date(y); f.setDate(f.getDate() - 6);
  return {
    from: Utilities.formatDate(f, WB_ADS_TZ_, 'yyyy-MM-dd'),
    to: Utilities.formatDate(y, WB_ADS_TZ_, 'yyyy-MM-dd')
  };
}
