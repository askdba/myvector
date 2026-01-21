#include "myvectorutils.h"
#include <string>

std::string quote_identifier(const std::string &identifier) {
  std::string quoted_identifier = "`";
  quoted_identifier += identifier;
  quoted_identifier += "`";
  return quoted_identifier;
}
