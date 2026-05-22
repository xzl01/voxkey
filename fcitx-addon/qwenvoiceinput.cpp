// SPDX-FileCopyrightText: 2026 HarryLoong
// SPDX-License-Identifier: MIT

#include <array>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <fcitx-utils/capabilityflags.h>
#include <fcitx-utils/event.h>
#include <fcitx-utils/eventloopinterface.h>
#include <fcitx-utils/log.h>
#include <fcitx-utils/utf8.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addonmanager.h>
#include <fcitx/inputcontext.h>
#include <fcitx/instance.h>

namespace {

constexpr const char *kSocketName = "qwen-voice-input-fcitx.sock";
constexpr size_t kMaxMessageSize = 65536;
constexpr std::string_view kCommitCommand = "COMMIT\n";
constexpr uint64_t kDuplicateCommitSuppressUs = 250000;

struct Request {
    enum class Type {
        Commit,
        Ping,
    };

    Type type = Type::Commit;
    std::string text;
};

bool isUsableRuntimeDirectory(const std::string &path) {
    struct stat st {};
    return stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode) &&
           access(path.c_str(), W_OK | X_OK) == 0;
}

std::string runtimeDir() {
    if (const char *runtime = std::getenv("XDG_RUNTIME_DIR")) {
        if (runtime[0] != '\0' && isUsableRuntimeDirectory(runtime)) {
            return runtime;
        }
    }
    std::string runUserDir = "/run/user/" + std::to_string(getuid());
    if (isUsableRuntimeDirectory(runUserDir)) {
        return runUserDir;
    }
    return "/tmp";
}

std::string socketPath() { return runtimeDir() + "/" + kSocketName; }

Request parseRequest(const std::string &message) {
    if (message.empty() || message == "PING" || message == "PING\n") {
        return {Request::Type::Ping, {}};
    }
    if (message.rfind(kCommitCommand, 0) == 0) {
        return {Request::Type::Commit,
                message.substr(kCommitCommand.size())};
    }

    // Backward compatibility for older daemons: a bare datagram is text.
    return {Request::Type::Commit, message};
}

class QwenVoiceInput final : public fcitx::AddonInstance {
public:
    explicit QwenVoiceInput(fcitx::AddonManager *manager)
        : instance_(manager->instance()) {
        setupSocket();
    }

    ~QwenVoiceInput() override {
        ioEvent_.reset();
        if (fd_ >= 0) {
            close(fd_);
            fd_ = -1;
        }
        if (socketBound_) {
            unlink(socketPath_.c_str());
        }
    }

private:
    void setupSocket() {
        socketPath_ = socketPath();
        sockaddr_un addr {};
        if (socketPath_.size() >= sizeof(addr.sun_path)) {
            FCITX_ERROR() << "Qwen voice input socket path is too long: "
                          << socketPath_;
            return;
        }

        fd_ = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (fd_ < 0) {
            FCITX_ERROR() << "Qwen voice input failed to create socket: "
                          << strerror(errno);
            return;
        }

        addr.sun_family = AF_UNIX;
        std::strncpy(addr.sun_path, socketPath_.c_str(),
                     sizeof(addr.sun_path) - 1);

        unlink(socketPath_.c_str());
        if (bind(fd_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
            FCITX_ERROR() << "Qwen voice input failed to bind " << socketPath_
                          << ": " << strerror(errno);
            close(fd_);
            fd_ = -1;
            return;
        }
        socketBound_ = true;
        chmod(socketPath_.c_str(), S_IRUSR | S_IWUSR);

        ioEvent_ = instance_->eventLoop().addIOEvent(
            fd_, fcitx::IOEventFlag::In,
            [this](fcitx::EventSourceIO *, int fd, fcitx::IOEventFlags flags) {
                return handleReadable(fd, flags);
            });
        FCITX_INFO() << "Qwen voice input fcitx socket listening on "
                     << socketPath_;
    }

    bool handleReadable(int fd, fcitx::IOEventFlags flags) {
        if (flags.testAny(fcitx::IOEventFlag::Err) ||
            flags.testAny(fcitx::IOEventFlag::Hup)) {
            FCITX_WARN() << "Qwen voice input socket event error/hangup";
        }

        while (true) {
            sockaddr_un peer {};
            socklen_t peerLen = sizeof(peer);
            std::array<char, kMaxMessageSize> buffer {};
            ssize_t len =
                recvfrom(fd, buffer.data(), buffer.size(), 0,
                         reinterpret_cast<sockaddr *>(&peer), &peerLen);

            if (len < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    return true;
                }
                FCITX_WARN() << "Qwen voice input recvfrom failed: "
                             << strerror(errno);
                return true;
            }

            std::string text(buffer.data(), static_cast<size_t>(len));
            handleMessage(text, peer, peerLen);
        }
    }

    void handleMessage(const std::string &text, const sockaddr_un &peer,
                       socklen_t peerLen) {
        Request request = parseRequest(text);

        if (request.type == Request::Type::Ping) {
            reply(peer, peerLen, "PONG");
            return;
        }

        if (request.text.empty()) {
            reply(peer, peerLen, "ERR empty-text");
            return;
        }
        if (request.text.find('\0') != std::string::npos) {
            reply(peer, peerLen, "ERR invalid-text");
            return;
        }

        fcitx::InputContext *ic = instance_->lastFocusedInputContext();
        if (!ic) {
            reply(peer, peerLen, "ERR no-focused-input-context");
            return;
        }

        commitText(ic, request.text);
        reply(peer, peerLen, "OK");
    }

    void commitText(fcitx::InputContext *ic, const std::string &text) {
        const uint64_t now = fcitx::now(CLOCK_MONOTONIC);
        const std::string program = ic->program();
        const std::string frontend(ic->frontendName());

        if (lastCommittedInputContext_ == ic &&
            lastCommittedText_ == text &&
            lastCommittedProgram_ == program &&
            lastCommittedFrontend_ == frontend &&
            now >= lastCommitTimeUs_ &&
            now - lastCommitTimeUs_ < kDuplicateCommitSuppressUs) {
            FCITX_WARN() << "Qwen voice input suppressed duplicate commit: "
                         << text;
            return;
        }

        if (ic->capabilityFlags() &
            fcitx::CapabilityFlag::CommitStringWithCursor) {
            ic->commitStringWithCursor(text, fcitx::utf8::length(text));
        } else {
            ic->commitString(text);
        }

        lastCommittedInputContext_ = ic;
        lastCommittedProgram_ = program;
        lastCommittedFrontend_ = frontend;
        lastCommittedText_ = text;
        lastCommitTimeUs_ = now;
        FCITX_INFO() << "Qwen voice input committed text via fcitx: " << text;
    }

    void reply(const sockaddr_un &peer, socklen_t peerLen,
               const char *message) {
        if (peerLen == 0 || peer.sun_path[0] == '\0') {
            return;
        }
        if (sendto(fd_, message, strlen(message), 0,
                   reinterpret_cast<const sockaddr *>(&peer), peerLen) < 0) {
            FCITX_WARN() << "Qwen voice input reply failed: "
                         << strerror(errno);
        }
    }

    fcitx::Instance *instance_ = nullptr;
    int fd_ = -1;
    bool socketBound_ = false;
    std::string socketPath_;
    std::unique_ptr<fcitx::EventSourceIO> ioEvent_;
    fcitx::InputContext *lastCommittedInputContext_ = nullptr;
    std::string lastCommittedProgram_;
    std::string lastCommittedFrontend_;
    std::string lastCommittedText_;
    uint64_t lastCommitTimeUs_ = 0;
};

class QwenVoiceInputFactory final : public fcitx::AddonFactory {
public:
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        return new QwenVoiceInput(manager);
    }
};

} // namespace

FCITX_ADDON_FACTORY(QwenVoiceInputFactory)
