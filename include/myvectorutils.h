#pragma once
#include <regex>
#include <string>
#include <unordered_map>
#include <vector>

/* A generic key-value map for options */
typedef std::unordered_map<std::string, std::string> OptionsMap;

/* Trim leading & trailing spaces */
inline std::string lrtrim(const std::string &str) {
  std::string ret = std::regex_replace(str, std::regex("^ +| +$|( ) +"), "$1");

  return ret;
}

/* Split a string at comma and return ordered list */
inline void split(const std::string &str, std::vector<std::string> &out) {
  static const std::regex re("[^,]+");

  std::sregex_iterator current(str.begin(), str.end(), re);
  std::sregex_iterator end;
  while (current != end) {
    std::string val = lrtrim((*current).str());
    out.push_back(val);
    current++;
  }
}

/* Helper class to manage vector index options as k-v map.
 * e.g type=HNSW,dim=1536,size=1000000,M=64,ef=100
 */
class MyVectorOptions {
private:
  /* Returns true on success, false on format error */
  bool parseKV(const char *line) {
    /* e.g options list-MYVECTOR(type=hnsw,dim=50,size=4000000,M=64,ef=100) */
    const char *ptr1 = line;
    if (strchr(line, '|')) /// start marker
    {
      ptr1 = strchr(line, '|');
      ptr1++;
    }
    std::string sline(ptr1);
    std::vector<std::string> listoptions;

    split(sline, listoptions);

    for (auto s : listoptions) {
      size_t eq = s.find_first_of('=');
      if (eq == std::string::npos) /// badly formed
        return false;

      std::string k = s.substr(0, eq);
      std::string v = s.substr(eq + 1);

      k = lrtrim(k);
      v = lrtrim(v);

      if (!k.length() || !v.length())
        return false;

      setOption(k, v);
    }

    return true;
  } /* parseKV */

public:
  MyVectorOptions(const std::string &options) {
    m_valid = false;
    if (parseKV(options.c_str()))
      m_valid = true;
  }

  bool isValid() const { return m_valid; }

  void setOption(const std::string &name, const std::string &val) {
    m_options[name] = val;
  }

  std::string getOption(const std::string &name) {
    std::string ret = "";
    if (m_options.find(name) != m_options.end())
      ret = m_options[name];
    return ret;
  }

  /* getIntOption - Safely parse an integer option with validation.
   * Returns the parsed value, or defaultVal if the option is missing or
   * invalid. Sets valid to false if the value exists but is not a valid
   * integer.
   */
  int getIntOption(const std::string &name, int defaultVal = 0,
                   bool *valid = nullptr) {
    std::string val = getOption(name);
    if (val.empty()) {
      if (valid)
        *valid = true; // Missing is OK, use default
      return defaultVal;
    }

    char *end = nullptr;
    long result = strtol(val.c_str(), &end, 10);

    // Check if entire string was consumed (valid integer)
    if (end == val.c_str() || *end != '\0') {
      if (valid)
        *valid = false;
      return defaultVal;
    }

    if (valid)
      *valid = true;
    return static_cast<int>(result);
  }

private:
  OptionsMap m_options;
  bool m_valid;
};

#ifdef TODO
/* Compare 2 binlog coordinates */
int binlogPositionCompare(const std::string &file1, size_t pos1,
                          const std::string &file2, size_t pos2) {
  if ((file2 == file1 && pos2 > pos1) || (file2 > file1))
    return 1;
  else if (file2 == file1 && pos2 == pos1)
    return 0;
  else
    return -1;
}

std::string quote_identifier(const std::string &identifier);
#endif
