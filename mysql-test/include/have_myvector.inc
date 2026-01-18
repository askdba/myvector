--let $have_myvector = `SELECT @@myvector_version IS NOT NULL`
--if (!$have_myvector) {
  --skip Tests require MyVector plugin
}
