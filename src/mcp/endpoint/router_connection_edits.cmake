# Edits to MySQL Router's http/server/connection.h (26.10), applied by CMakeLists.txt to a copy
# in the build directory; the server tree is not modified. Each anchor must occur exactly once
# in the original, or the configure step fails (a MySQL release that changed the file).
#
# What they add (askdba/myvector#269, #304): http::server::DeferrableConnection, through which
# component_mcp answers a request later from a worker thread, and an idle bound per connection
# (http::server::idle_timeout_ms) that also covers the TLS handshake.

SET(MCP_ROUTER_EDIT_1_ANCHOR [==[#include <charconv>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>

#include "http/base/connection.h"
#include "http/base/headers.h"
#include "http/server/http_counters.h"
#include "http/server/request.h"
#include "http/server/request_handler_interface.h"
#include "mysqlrouter/http_server_lib_export.h"
#include "mysqlrouter/uri.h"

namespace http {
namespace server {

template <typename Socket>
class ServerConnection : public http::base::Connection<Socket> {]==])
SET(MCP_ROUTER_EDIT_1_TEXT [==[#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <utility>

#include "http/base/connection.h"
#include "http/base/headers.h"
#include "http/server/http_counters.h"
#include "http/server/request.h"
#include "http/server/request_handler_interface.h"
#include "mysqlrouter/http_server_lib_export.h"
#include "mysqlrouter/uri.h"
#include "mysql/harness/net_ts/executor.h"
#include "mysql/harness/net_ts/timer.h"

namespace http {
namespace server {

// component_mcp (askdba/myvector): replies sent later, from another thread (#304), and a bound on
// the time a connection may go without completing a request, TLS handshake included (#269).
class DeferrableConnection {
 public:
  virtual ~DeferrableConnection() = default;
  // On the connection's I/O thread, while a request is routed: read no further requests until
  // resume_reading(), so that replies keep their order and pipelined requests stay few.
  virtual void pause_reading() = 0;
  // On the connection's I/O thread: read requests again (and restart the idle bound).
  virtual void resume_reading() = 0;
  // Unique for the server's lifetime (a connection's address may be reused after it closes).
  virtual uint64_t connection_id() const = 0;
  using Task = std::function<void(DeferrableConnection *)>;
  // From any thread: runs a task on the connection's I/O thread, if the connection still exists
  // by then (it is passed to the task); otherwise drops it.
  virtual std::function<void(Task)> poster() = 0;
};

// Milliseconds a connection may stay open without completing a request: from its start, and
// from each reply. 0: no bound.
inline std::atomic<uint64_t> idle_timeout_ms{0};
// Set while the server stops: a connection whose deferred reply has been sent closes instead of
// reading again (Server::stop() only cancels pending I/O, and a paused connection has none).
inline std::atomic<bool> stopping{false};
inline std::atomic<uint64_t> next_connection_id{1};

template <typename Socket>
class ServerConnection
    : public http::base::Connection<Socket>,
      public DeferrableConnection,
      public std::enable_shared_from_this<ServerConnection<Socket>> {]==])

SET(MCP_ROUTER_EDIT_2_ANCHOR [==[        request_handler_{rhi},
        connection_handler_{connection_handler} {}]==])
SET(MCP_ROUTER_EDIT_2_TEXT [==[        request_handler_{rhi},
        connection_handler_{connection_handler},
        idle_timer_{http::base::impl::get_socket(&this->socket_)->get_executor().context()} {}

  uint64_t connection_id() const override { return id_; }

  void start() override {
    arm_idle_timer();
    Parent::start();
  }

  void pause_reading() override {
    paused_ = true;
    idle_timer_.cancel();
    this->suspend();
  }

  void resume_reading() override {
    if (!paused_) return;
    paused_ = false;
    this->resume();
    if (!this->running_) return;  // stopping: the pending I/O closes the connection
    if (!this->keep_alive_ || stopping.load()) {
      // The last reply ended the connection ("Connection: close"), or the server stops: close it
      // as the read path does, after the reply is written if it is still being sent.
      // Inside the receive path (a request answered while it is routed), that path closes it.
      if (!this->stop_running() && !this->reading_pending_)
        Parent::connection_handler_->on_connection_close(this);
      return;
    }
    arm_idle_timer();
    if (!this->reading_pending_) this->do_net_recv();
  }

  std::function<void(Task)> poster() override {
    auto *ctx = &http::base::impl::get_socket(&this->socket_)->get_executor().context();
    return [weak = this->weak_from_this(), ctx](Task task) {
      net::post(*ctx, [weak, task = std::move(task)]() {
        if (auto self = weak.lock()) task(self.get());
      });
    };
  }]==])

SET(MCP_ROUTER_EDIT_3_ANCHOR [==[    if (request_handler_) {
      request_handler_->route(it->second.request);
    }

    return 0;
  }]==])
SET(MCP_ROUTER_EDIT_3_TEXT [==[    idle_timer_.cancel();
    if (request_handler_) {
      request_handler_->route(it->second.request);
    }
    // Replied already (not deferred): the idle bound runs again from here.
    if (!paused_) arm_idle_timer();

    return 0;
  }]==])

SET(MCP_ROUTER_EDIT_4_ANCHOR [==[  bool first_request_{true};]==])
SET(MCP_ROUTER_EDIT_4_TEXT [==[  void arm_idle_timer() {
    const uint64_t ms = idle_timeout_ms.load();
    if (ms == 0) return;
    idle_timer_.expires_after(std::chrono::milliseconds(ms));
    idle_timer_.async_wait([weak = this->weak_from_this()](std::error_code ec) {
      if (ec) return;  // cancelled: re-armed, a request completed, or the connection is gone
      if (auto self = weak.lock()) self->on_idle_timeout();
    });
  }

  // Ends the pending read (or TLS handshake): the connection closes through its usual path.
  void on_idle_timeout() {
    if (paused_) return;
    http::base::impl::get_socket(&this->socket_)->cancel();
  }

  bool paused_{false};
  bool first_request_{true};]==])

SET(MCP_ROUTER_EDIT_5_ANCHOR [==[  ConnectionStatusCallbacks *connection_handler_;
};]==])
SET(MCP_ROUTER_EDIT_5_TEXT [==[  ConnectionStatusCallbacks *connection_handler_;
  net::steady_timer idle_timer_;
  const uint64_t id_{next_connection_id++};
};]==])

SET(MCP_ROUTER_EDITS 5)
