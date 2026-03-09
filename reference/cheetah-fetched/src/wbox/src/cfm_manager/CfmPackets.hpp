#pragma once

#include "CfmTypes.hpp"

extern "C" {
#include "sdk_common/wb_packet.h"
}

/* if you want to log the content of a packet above the sdk wrap, please use this log */
#define FMT_LOG_CFM_PACKET(__msg__, buffer, buff_len)                       \
    if (g_cfm_pdu_debug_flag)                                               \
    {                                                                       \
        std::string log_str = __msg__;                                      \
        for (uint16_t i = 0; i < buff_len; ++i)                             \
        {                                                                   \
            log_str += fmt::format("{:02x} ", static_cast<int>(buffer[i])); \
        }                                                                   \
        CFM_LOG(DN_LOG_DEBUG, "%s", log_str.c_str());                       \
    }

static constexpr uint8_t OAM_OPCODE_CCM = 1;
static constexpr uint8_t OAM_OPCODE_LBR = 2;
static constexpr uint8_t OAM_OPCODE_LBM = 3;
static constexpr uint8_t OAM_OPCODE_LTR = 4;
static constexpr uint8_t OAM_OPCODE_LTM = 5;
static constexpr uint8_t OAM_OPCODE_LMM = 43;
static constexpr uint8_t OAM_OPCODE_DMR = 46;
static constexpr uint8_t OAM_OPCODE_DMM = 47;
static constexpr uint8_t OAM_OPCODE_SLR = 54;
static constexpr uint8_t OAM_OPCODE_SLM = 55;

// CFM
static constexpr uint8_t CFM_TLV_POS = 4;
static constexpr uint8_t TLV_HEADER_SIZE = 3;
static constexpr uint8_t MAID_LEN = 48;

// LTM
static constexpr uint8_t LTM_EGRESS_IDENTIFIER_TLV_TYPE = 7;
static constexpr uint16_t LTM_EGRESS_IDENTIFIER_TLV_LENGTH = 8;
static constexpr uint8_t LTM_FIRST_TLV_OFFSET = 17;

// LTR
static constexpr uint8_t LTR_REPLY_INGRESS_TLV_TYPE = 5;
static constexpr uint8_t LTR_REPLY_INGRESS_TLV_LENGTH = 7;
static constexpr uint8_t LTR_REPLY_EGRESS_TLV_TYPE = 6;
static constexpr uint8_t LTR_REPLY_EGRESS_TLV_LENGTH = 7;
static constexpr uint8_t LTR_EGRESS_IDENTIFIER_TLV_TYPE = 8;
static constexpr uint16_t LTR_EGRESS_IDENTIFIER_TLV_LENGTH = 16;
static constexpr uint8_t LTR_FIRST_TLV_OFFSET = 6;

//LBM
static constexpr uint8_t DATA_TLV_TYPE = 3;
static constexpr uint8_t END_TLV = 0;
static constexpr uint8_t LBM_FIRST_TLV_OFFSET = 4;
static constexpr uint16_t LBM_MIN_PDU_SIZE = 9;
static constexpr uint16_t LBM_MIN_PDU_SIZE_WITH_DATA_TLV = 12;

namespace cfm {

enum ReplyAction
{
    OK = 1,
    Down,
    Blocked,
    VID
};

enum RelayAction
{
    RlyHit = 1,
    RlyFDB,
    RlyMPDB
};

struct CfmHeader
{
    uint8_t md_info;
    uint8_t opcode;
    uint8_t flags;
    uint8_t first_tlv_offset;
} __attribute__((__packed__));

struct CcmHeader
{
    CfmHeader cfm_hdr{};
    uint32_t sequence_number;
    uint16_t mep_id;
    uint8_t maid[MAID_LEN];
} __attribute__((__packed__));

struct LtmHeader
{
    CfmHeader cfm_hdr{};
    uint32_t transaction_id;
    uint8_t ttl;
    struct ether_addr original_mac;
    struct ether_addr target_mac;
} __attribute__((__packed__));

struct SlmHeader
{
    CfmHeader cfm_hdr{};
    uint16_t src_mep_id;
    uint16_t res_dest_mep_id;
    uint32_t test_id;
    uint32_t my_tx;
    uint32_t res_remote_tx;
} __attribute__((__packed__));

struct SlrHeader
{
    CfmHeader cfm_hdr{};
    uint16_t src_mep_id;
    uint16_t dest_mep_id;
    uint32_t test_id;
    uint32_t my_tx;
    uint32_t remote_tx;
} __attribute__((__packed__));

struct LtrHeader
{
    CfmHeader cfm_hdr;
    uint32_t transaction_id;
    uint8_t ttl;
    uint8_t relay_action;
} __attribute__((__packed__));

struct LtmEgressIdentifierTlv
{
    uint8_t type{LTM_EGRESS_IDENTIFIER_TLV_TYPE};
    uint16_t length{ntohs(LTM_EGRESS_IDENTIFIER_TLV_LENGTH)};
    uint16_t initiator_id;
    struct ether_addr initiator_mac;
} __attribute__((__packed__));

struct LtrEgressIdentifierTlv
{
    uint8_t type{LTR_EGRESS_IDENTIFIER_TLV_TYPE};
    uint16_t length{ntohs(LTR_EGRESS_IDENTIFIER_TLV_LENGTH)};
    uint16_t last_egress_id;
    struct ether_addr last_egress_mac;
    uint16_t next_egress_id;
    struct ether_addr next_egress_mac;
} __attribute__((__packed__));

struct LtrReplyIngressTlv
{
    uint8_t type{LTR_REPLY_INGRESS_TLV_TYPE};
    uint16_t length{ntohs(LTR_REPLY_INGRESS_TLV_LENGTH)};
    uint8_t ingress_action{ReplyAction::OK};
    struct ether_addr ingress_mac;
} __attribute__((__packed__));

struct LtrReplyEgressTlv
{
    uint8_t type{LTR_REPLY_EGRESS_TLV_TYPE};
    uint16_t length{ntohs(LTR_REPLY_EGRESS_TLV_LENGTH)};
    uint8_t egress_action{ReplyAction::OK};
    struct ether_addr egress_mac;
} __attribute__((__packed__));

struct DataTlv
{
    uint8_t type{DATA_TLV_TYPE};
    uint16_t length;
} __attribute__((__packed__));

struct LbmHeader
{
    CfmHeader cfm_hdr{};
    uint32_t transaction_id;
    //reserved for future: len 0, after transaction_id
} __attribute__((__packed__));


class CfmPacket
{
public:
    CfmPacket();
    CfmPacket(wb_pkt* wrap_pkt);
    inline wb_pkt* GetWbPacket() const { return pkt; }
    inline uint8_t GetOpcode() const { return cfm_hdr->opcode; }

    struct ether_hdr* ether_hdr{nullptr};
    struct vlan_hdr* inner_vlan_hdr{nullptr};
    struct vlan_hdr* outer_vlan_hdr{nullptr};
    CfmHeader* cfm_hdr{nullptr};
    uint8_t l2_size{0};
    uint8_t md_level{0};

protected:
    void ParseL2Header();
    void ConfigureL2Header(const ether_addr* my_mac,
                           const ether_addr* dst_mac,
                           int inner_vlan,
                           int outer_vlan,
                           int pcp,
                           uint16_t inner_tpid,
                           uint16_t outer_tpid);
    wb_pkt* pkt{nullptr};
};

class CcmPacket : public CfmPacket
{
public:
    CcmPacket(wb_pkt* wrap_pkt);

    CcmHeader* ccm_hdr{nullptr};
    bool rdi{false};
    bool traffic{false};
    CcmIntervalType ccm_interval;
};

class SlrPacket : public CfmPacket
{
public:
    SlrPacket(wb_pkt* wrap_pkt);

    SlrHeader* slr_hdr{nullptr};
    uint16_t src_mep_id;
    uint16_t dest_mep_id;
    uint32_t test_id;
    uint32_t my_tx;
    uint32_t remote_tx;
};

class SlmPacket : public CfmPacket
{
public:
    SlmPacket(const ether_addr* my_mac,
              const ether_addr* dst_mac,
              int inner_vlan,
              int outer_vlan,
              int pcp,
              uint8_t level,
              uint16_t source_mep,
              uint32_t test_id,
              uint32_t my_tx,
              uint16_t inner_tpid,
              uint16_t outer_tpid);
    int Send(int direction, int lmep_hw_id);

    SlmHeader* slm_hdr{nullptr};

private:
    void ConfigureSLMHeader(uint8_t level,
                            uint16_t source_mep,
                            uint32_t test_id,
                            uint32_t my_tx);
};

class LtmPacket : public CfmPacket
{
public:
    LtmPacket();
    LtmPacket(wb_pkt* wrap_pkt);
    LtmPacket(const ether_addr* my_mac,
              const ether_addr* dst_mac,
              int inner_vlan,
              int outer_vlan,
              int pcp,
              uint8_t level,
              uint32_t transaction_id,
              uint8_t ttl,
              const ether_addr* target_mac,
              uint16_t inner_tpid,
              uint16_t outer_tpid);
    bool PrepareForward(const ether_addr* my_cfm_mac);
    bool Send(const std::string& iface_name) const;
    bool Send(int direction, int lmep_hw_id) const;
    LtmHeader* ltm_hdr{nullptr};
    LtmEgressIdentifierTlv* ltm_egress_identifier_tlv{nullptr};
    bool use_fdb_only{false};
    bool is_legacy_ltm{true};
    std::vector<uint8_t> additional_tlv;

private:
    void ConfigureLTMHeader(uint8_t level,
                            uint32_t transaction_id,
                            uint8_t ttl,
                            const ether_addr* original_mac,
                            const ether_addr* target_mac);
    void ConfigureEgressIdentifierTlv(uint16_t initiator_id,
                                      const ether_addr* initiator_mac);
};

class LtrPacket : public CfmPacket
{
public:
    LtrPacket(const LtmPacket& ltm,
              uint16_t direction,
              const ether_addr* my_cfm_mac);
    LtrPacket(wb_pkt* wrap_pkt);
    ~LtrPacket();
    void SetFlags(bool use_fdb_only, bool fwd_yes, bool terminal_mep);
    void Send(stw_t* timer_wheel);
    static void SendDelayLtrCb(stw_tmr_t* tmr, void* ltm_wb_pkt);

    LtrHeader* ltr_hdr{nullptr};
    LtrEgressIdentifierTlv* ltr_egress_identifier_tlv{nullptr};
    LtrReplyIngressTlv* ltr_reply_ingress_tlv{nullptr};
    LtrReplyEgressTlv* ltr_reply_egress_tlv{nullptr};
    bool is_sent{false};
};


class LoopbackPayload
{
public:
    LoopbackPayload(wb_pkt* wrap_pkt,
                    const uint8_t opcode,
                    const uint8_t l2_size);
    LoopbackPayload(uint8_t level,
                    uint32_t transaction_id,
                    const uint16_t length);

    void FillPacket(wb_pkt* wrap_pkt, const uint8_t l2_size);

    ~LoopbackPayload();
    bool operator==(const LoopbackPayload& other) const;

    LbmHeader lbm_hdr;
    DataTlv data_tlv = {0, 0};
    uint8_t* data_tlv_payload{nullptr};
    uint8_t end_tlv{END_TLV};
    uint16_t data_tlv_length{0};
    bool has_data_tlv{false};
};

class LbmPacket : public CfmPacket
{
public:
    LbmPacket(wb_pkt* wrap_pkt);
    LbmPacket(const ether_addr* my_mac,
              const ether_addr* dst_mac,
              int inner_vlan,
              int outer_vlan,
              int pcp,
              uint8_t level,
              uint32_t transaction_id,
              uint16_t length,
              uint16_t inner_tpid,
              uint16_t outer_tpid);
    bool Send(int direction, int lmep_hw_id);

    // Method to transfer ownership of lbm_payload
    std::unique_ptr<LoopbackPayload> ReleaseLbmPayload()
    {
        return std::move(lbm_payload);
    };

private:
    std::unique_ptr<LoopbackPayload> lbm_payload;
};


class LbrPacket : public CfmPacket
{
public:
    LbrPacket(wb_pkt* wrap_pkt);
    ~LbrPacket();

    inline uint32_t GetTransactionId() const { return transaction_id; }

    // Method to transfer ownership of lbm_payload
    std::unique_ptr<LoopbackPayload> ReleaseLbmPayload()
    {
        return std::move(lbm_payload);
    };

private:
    std::unique_ptr<LoopbackPayload> lbm_payload;
    uint32_t transaction_id;
};

} // namespace cfm
