#pragma once

#include <rte_ether.h>

#include <cmath>
#include <cstdint>
#include <optional>
#include <string>
#include <variant>

enum SessionState
{
    SESS_STATE_INITIALIZING,     // Constructed, but not started
    SESS_STATE_RUNNING,          // Has packets to send
    SESS_STATE_WAIT_LAST_PACKET, // Finished TX, wait for last RX
    SESS_STATE_DONE, // Session done, ready to be removed from hashes (not deleted)
    SESS_STATE_ABORT, // Session ready to be deleted, if no one holds a reference
};

enum SessionOpcode
{
    SESS_DMM = 1,
    SESS_SLM,
    SESS_LBM,
    SESS_LTM,
    SESS_UNKNOWN,
};

enum SessionStartStatus
{
    SESS_START_OK = 1,
    SESS_START_ERR_EXISTS,
    SESS_START_ERR_MISSING_MEP,
    SESS_START_ERR_DISABLED_MEP,
    SESS_START_ERR_MISSING_MAC,
    SESS_START_ERR_COMMIT_PROGRESS,
    SESS_START_ERR_UNSUPPORTED,
    SESS_START_ERR,
};

enum SessionStopStatus
{
    SESS_STOP_OK = 1,
    SESS_STOP_ERR,
};

struct SessionStartResponse
{
    uint64_t sess_id;
    SessionStartStatus status;
};

struct Proactive
{
    struct DmmThreshold
    {
        std::optional<uint32_t> delay_rtt_min;
        std::optional<uint32_t> delay_rtt_avg;
        std::optional<uint32_t> delay_rtt_max;
        std::optional<uint32_t> jitter_rtt_avg;
        std::optional<uint32_t> jitter_rtt_max;
        std::optional<float_t> success_rate_pcnt;
    };

    struct SlmThreshold
    {
        std::optional<float_t> near_end_loss;
        std::optional<float_t> far_end_loss;
    };

    using EventThreshold =
        std::optional<std::variant<DmmThreshold, SlmThreshold>>;

    std::string name;
    uint32_t id; // required for SNMP indexing where `name` cannot be used
    uint64_t created_ts;
    EventThreshold event_threshold;
    uint32_t result_index = 0;
};

struct SessionStartRequest
{
    SessionOpcode type;
    std::optional<Proactive> proactive;
    uint32_t oam_id;
    std::optional<uint32_t> rmep_id = {};
    ether_addr dmac = {};
    uint32_t interval_ms = 1000;
    uint32_t pkt_count = 10;
    uint32_t frame_size = 0;
    uint32_t pcp = 7;
    uint32_t max_hops = 64;
};

struct SessionStopRequest
{
    uint64_t sess_id;
};

struct SessionStopResponse
{
    uint64_t sess_id;
    SessionStopStatus status;
};

static inline uint64_t sess_compute_id(uint32_t oam_id, uint32_t proto)
{
    return ((uint64_t)oam_id << 32 | proto);
}

static inline SessionOpcode sess_get_opcode(uint64_t sess_id)
{
    return ((SessionOpcode)((uint32_t)sess_id & 0xFFFFFFFF));
}

static inline uint32_t sess_get_oamid(uint64_t sess_id)
{
    return ((uint32_t)(sess_id >> 32 & 0xFFFFFFFF));
}

static inline const char* sess_get_opcode_str(SessionOpcode opcode)
{
    switch (opcode)
    {
    case SESS_DMM:
        return "DMM";
    case SESS_SLM:
        return "SLM";
    case SESS_LBM:
        return "LBM";
    case SESS_LTM:
        return "LTM";
    default:
        return "Unknown";
    }
}

static inline const char* sess_get_state_str(SessionState state)
{
    switch (state)
    {
    case SESS_STATE_INITIALIZING:
        return "Init";
    case SESS_STATE_RUNNING:
        return "Run";
    case SESS_STATE_WAIT_LAST_PACKET:
        return "LastPacket";
    case SESS_STATE_DONE:
        return "Done";
    case SESS_STATE_ABORT:
        return "Abort";
    default:
        return "Unknown";
    }
}
