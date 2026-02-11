#ifndef MYVECTOR_BINLOG_SERVICE_H
#define MYVECTOR_BINLOG_SERVICE_H

#include <mysql/components/my_service.h>
#include <mysql/components/component_implementation.h>

namespace myvector_component {

class MyVectorBinlogService : public mysql::mysql_service_base {
public:
    // Service interface methods
    const char* get_name() const override { return "MyVector Binlog Service"; }
    const char* get_description() const override { return "Monitors MySQL binlog for MyVector updates."; }
    uint32_t get_version() const override { return 1; }

    // Custom methods for starting and stopping the binlog thread
    virtual int start_binlog_monitoring() = 0;
    virtual int stop_binlog_monitoring() = 0;

protected:
    // Destructor is protected to ensure proper memory management through factory methods
    ~MyVectorBinlogService() = default;
};

SERVICE_INTERFACE_VERSION(MyVectorBinlogService, 1);

} // namespace myvector_component

#endif // MYVECTOR_BINLOG_SERVICE_H
