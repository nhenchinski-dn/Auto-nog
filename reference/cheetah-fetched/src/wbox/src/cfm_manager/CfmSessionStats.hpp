#pragma once

#include <map>
#include <string>

namespace cfm {

struct CfmSessionStatsSLM
{
    uint32_t my_tx;
    uint32_t my_rx;
    uint32_t remote_tx;
    uint32_t remote_rx;
    uint32_t loss_far_end;
    uint32_t loss_near_end;
    uint32_t my_tx_slr;
    uint32_t remote_tx_slr;
    float loss_far_end_p;
    float loss_near_end_p;
};

struct CfmSessionStatsLTM
{
    struct LTREntry
    {
        std::string src_mac_address;
        std::string last_egress_id;
        std::string last_egress_mac_address;
        std::string next_egress_id;
        std::string next_egress_mac_address;
        std::string reply_mac_address;
        std::string reply_action;
        std::string relay_action;
    };

    using TTL_LTREntry_Map = std::map<int, LTREntry, std::greater<>>;

    uint32_t my_tx;
    uint32_t my_rx;
    uint32_t transaction_id;
    TTL_LTREntry_Map hops;
};

struct CfmSessionStatsLBM
{
    uint32_t tx_count;
    uint32_t rx_count;
    uint32_t rx_good_count;
    uint32_t rx_bad_order_count;
    uint32_t rx_malformed_count;
    uint32_t rx_timeout_count;
    uint32_t rx_anomaly_count; // for our eyes only, not exposed to the user
};

struct CfmSessionStatsDMM
{
    uint64_t tx_count;
    uint64_t rx_count;
    uint64_t last_delay;
    uint64_t delay_min;
    uint64_t delay_max;
    uint64_t delay_avg;
    uint64_t delay_variation;
    uint64_t delay_variation_max;
    uint64_t jitter;
};

} // namespace cfm
