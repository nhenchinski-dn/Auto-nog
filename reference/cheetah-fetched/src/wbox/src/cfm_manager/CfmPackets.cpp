#include "CfmPackets.hpp"

#include "CfmUtils.hpp"

extern "C" {
#include "forwarding_manager/forwarding_processor/fib_api.h"
#include "libdatapath/packet_parser/packet_parser.h"
#include "sdk_wrap/sdk_common/wb_packet_pool.h"
}

#include <spdlog/fmt/fmt.h>

#include <random>

namespace cfm {

CfmPacket::CfmPacket()
{
    if (unlikely(packet_pool_allocate(&pkt, 1)))
    {
        throw std::runtime_error("Failed to allocate wb_packet");
    }

    ether_hdr = reinterpret_cast<struct ether_hdr*>(pkt->data);
}

CfmPacket::CfmPacket(wb_pkt* wrap_pkt) : pkt(wrap_pkt)
{
    ParseL2Header();
    cfm_hdr = reinterpret_cast<CfmHeader*>(wrap_pkt->data + l2_size);
    md_level = cfm_hdr->md_info >> 5;
}

void CfmPacket::ParseL2Header()
{
    if (unlikely(pkt == nullptr))
    {
        throw std::invalid_argument("pkt is nullptr");
    }

    if (unlikely(pkt->data == nullptr))
    {
        throw std::invalid_argument("pkt->data is nullptr");
    }

    uint16_t eth_type = 0;
    uint16_t inner_vlan = 0;
    uint16_t outer_vlan = 0;

    parse_l2_components(pkt->data,
                        pkt->data_len,
                        &inner_vlan,
                        &outer_vlan,
                        &l2_size,
                        &eth_type);

    if (unlikely(eth_type != NET_ETHER_TYPE_OAM))
    {
        throw std::runtime_error("Failed to find CFM frame in packet!");
    }

    ether_hdr = reinterpret_cast<struct ether_hdr*>(pkt->data);

    if (inner_vlan)
    {
        inner_vlan_hdr = reinterpret_cast<struct vlan_hdr*>(
            pkt->data + sizeof(struct ether_hdr));
    }

    if (inner_vlan && outer_vlan)
    {
        outer_vlan_hdr = reinterpret_cast<struct vlan_hdr*>(
            pkt->data + sizeof(struct ether_hdr) + sizeof(struct vlan_hdr));
    }
}

void CfmPacket::ConfigureL2Header(const ether_addr* my_mac,
                                  const ether_addr* dst_mac,
                                  int inner_vlan,
                                  int outer_vlan,
                                  int pcp,
                                  uint16_t inner_tpid,
                                  uint16_t outer_tpid)
{
    ether_addr_copy(my_mac, &this->ether_hdr->s_addr);
    ether_addr_copy(dst_mac, &ether_hdr->d_addr);
    ether_hdr->ether_type = NET_ETHER_TYPE_OAM;
    l2_size = sizeof(struct ether_hdr);

    if (outer_vlan)
    {
        outer_vlan_hdr =
            reinterpret_cast<struct vlan_hdr*>(pkt->data + l2_size);
        ether_hdr->ether_type = rte_cpu_to_be_16(outer_tpid);
        outer_vlan_hdr->eth_proto = NET_ETHER_TYPE_OAM;
        uint16_t vlan = (pcp & 0x7) << 13 | outer_vlan;
        outer_vlan_hdr->vlan_tci = rte_cpu_to_be_16(vlan);
        l2_size += sizeof(struct vlan_hdr);
        if (inner_vlan)
        {
            inner_vlan_hdr =
                reinterpret_cast<struct vlan_hdr*>(pkt->data + l2_size);
            outer_vlan_hdr->eth_proto = rte_cpu_to_be_16(inner_tpid);
            inner_vlan_hdr->eth_proto = NET_ETHER_TYPE_OAM;
            uint16_t vlan = (pcp & 0x7) << 13 | inner_vlan;
            inner_vlan_hdr->vlan_tci = rte_cpu_to_be_16(vlan);
            l2_size += sizeof(struct vlan_hdr);
        }
    }

    pkt->data_len = l2_size;
}

CcmPacket::CcmPacket(wb_pkt* wrap_pkt) : CfmPacket(wrap_pkt)
{
    ccm_hdr = reinterpret_cast<CcmHeader*>(pkt->data + l2_size);

    if (unlikely(ccm_hdr->cfm_hdr.opcode != OAM_OPCODE_CCM))
    {
        throw std::runtime_error("Failed to find CCM frame in packet!");
    }

    rdi = ccm_hdr->cfm_hdr.flags & 0b10000000;
    traffic = ccm_hdr->cfm_hdr.flags & 0b01000000;
    ccm_interval =
        static_cast<CcmIntervalType>(ccm_hdr->cfm_hdr.flags & 0b00000111);
}

SlrPacket::SlrPacket(wb_pkt* wrap_pkt) : CfmPacket(wrap_pkt)
{
    slr_hdr = reinterpret_cast<SlrHeader*>(pkt->data + l2_size);

    if (unlikely(slr_hdr->cfm_hdr.opcode != OAM_OPCODE_SLR))
    {
        throw std::runtime_error("Failed to find SLR frame in packet!");
    }

    src_mep_id = rte_be_to_cpu_16(slr_hdr->src_mep_id);
    dest_mep_id = rte_be_to_cpu_16(slr_hdr->dest_mep_id);
    test_id = rte_be_to_cpu_32(slr_hdr->test_id);
    my_tx = rte_be_to_cpu_32(slr_hdr->my_tx);
    remote_tx = rte_be_to_cpu_32(slr_hdr->remote_tx);
}

LtmPacket::LtmPacket()
{
    ltm_hdr = reinterpret_cast<LtmHeader*>(pkt->data + sizeof(ether_hdr));
}

LtmPacket::LtmPacket(wb_pkt* wrap_pkt) : CfmPacket(wrap_pkt)
{
    ltm_hdr = reinterpret_cast<LtmHeader*>(pkt->data + l2_size);

    if (unlikely(ltm_hdr->cfm_hdr.opcode != OAM_OPCODE_LTM))
    {
        throw std::runtime_error("Failed to find LTM frame in packet!");
    }

    use_fdb_only = ltm_hdr->cfm_hdr.flags & 0b10000000;

    // Extract LTM Egress Identifier TLV
    uint16_t next_tlv_offset =
        l2_size + CFM_TLV_POS + ltm_hdr->cfm_hdr.first_tlv_offset;

    do
    {
        uint16_t tlv_length = 0;
        memcpy(
            &tlv_length, pkt->data + next_tlv_offset + 1, sizeof(tlv_length));

        if (pkt->data[next_tlv_offset] == LTM_EGRESS_IDENTIFIER_TLV_TYPE)
        {
            ltm_egress_identifier_tlv =
                reinterpret_cast<LtmEgressIdentifierTlv*>(pkt->data
                                                          + next_tlv_offset);
            is_legacy_ltm = false;
        }
        else
        {
            additional_tlv.insert(additional_tlv.end(),
                                  pkt->data + next_tlv_offset,
                                  pkt->data + next_tlv_offset + TLV_HEADER_SIZE
                                      + ntohs(tlv_length));
        }

        next_tlv_offset += TLV_HEADER_SIZE + ntohs(tlv_length);

    } while (pkt->data[next_tlv_offset] != 0);
}

LtmPacket::LtmPacket(const ether_addr* my_mac,
                     const ether_addr* dst_mac,
                     int inner_vlan,
                     int outer_vlan,
                     int pcp,
                     uint8_t level,
                     uint32_t transaction_id,
                     uint8_t ttl,
                     const ether_addr* target_mac,
                     uint16_t inner_tpid,
                     uint16_t outer_tpid)
    : CfmPacket()
{
    ConfigureL2Header(
        my_mac, dst_mac, inner_vlan, outer_vlan, pcp, inner_tpid, outer_tpid);
    ConfigureLTMHeader(level, transaction_id, ttl, my_mac, target_mac);
}

void LtmPacket::ConfigureLTMHeader(uint8_t level,
                                   uint32_t transaction_id,
                                   uint8_t ttl,
                                   const ether_addr* original_mac,
                                   const ether_addr* target_mac)
{
    ltm_hdr = reinterpret_cast<LtmHeader*>(pkt->data + l2_size);

    ltm_hdr->cfm_hdr.md_info = level << 5;
    ltm_hdr->cfm_hdr.flags = 8; // set to UseFDBonly
    ltm_hdr->cfm_hdr.first_tlv_offset =
        LTM_FIRST_TLV_OFFSET; // 17 bytes for Egress Identifier TLV  (Transaction ID (4) + TTL (1) + Original MAC (6) + Target MAC (6))
    ltm_hdr->cfm_hdr.opcode = OAM_OPCODE_LTM;

    ltm_hdr->transaction_id = ntohl(transaction_id);
    ltm_hdr->ttl = ttl;
    ether_addr_copy(original_mac, &ltm_hdr->original_mac);
    ether_addr_copy(target_mac, &ltm_hdr->target_mac);

    pkt->data_len += sizeof(LtmHeader);

    ConfigureEgressIdentifierTlv((uint16_t)transaction_id, original_mac);
}

void LtmPacket::ConfigureEgressIdentifierTlv(uint16_t initiator_id,
                                             const ether_addr* initiator_mac)
{
    ltm_egress_identifier_tlv =
        reinterpret_cast<LtmEgressIdentifierTlv*>(pkt->data + pkt->data_len);
    pkt->data_len += sizeof(LtmEgressIdentifierTlv);

    ltm_egress_identifier_tlv->type = LTM_EGRESS_IDENTIFIER_TLV_TYPE;
    ltm_egress_identifier_tlv->length = ntohs(LTM_EGRESS_IDENTIFIER_TLV_LENGTH);
    ltm_egress_identifier_tlv->initiator_id = initiator_id;
    ether_addr_copy(initiator_mac, &ltm_egress_identifier_tlv->initiator_mac);

    pkt->data[pkt->data_len] = 0;
    ++pkt->data_len;
}

bool LtmPacket::PrepareForward(const ether_addr* my_cfm_mac)
{
    ltm_hdr->ttl--;
    ether_addr_copy(my_cfm_mac, &ether_hdr->s_addr);

    if (!is_legacy_ltm)
    {
        ltm_egress_identifier_tlv->initiator_id = 0;
        ether_addr_copy(my_cfm_mac, &ltm_egress_identifier_tlv->initiator_mac);
    }

    return true;
}

bool LtmPacket::Send(const std::string& iface_name) const
{
    if (pkt->cfm_info.direction)
    {
        if (ltm_hdr->ttl > 0)
        {
            // send down
            SDK_WRAP_API(cfm, cfm_tx, pkt->cfm_info.lmep_hw_id, 0, pkt);
        }
    }
    else
    {
        // send up
        cfm_mac_resolution_data data;
        cfm_mac_resolution_t resolution = FibL2ServiceGetCfmMacResolution(
            GetMgmtId(iface_name), &ltm_hdr->target_mac, &data);
        // TODO: replace interface names with mgmt_id

        if (ltm_hdr->ttl == 0)
        {
            // Return true of destination is reachable, even if the packet was not sent because of expired TTL
            return (resolution == CFM_MAC_RESOLUTION_SUCCESS
                    || resolution == CFM_MAC_RESOLUTION_SEND_WITHOUT_MACTABLE);
        }

        if (resolution == CFM_MAC_RESOLUTION_SUCCESS)
        {
            SDK_WRAP_API(cfm, cfm_tx_ltm, &data, pkt);
        }
        else if (resolution == CFM_MAC_RESOLUTION_SEND_WITHOUT_MACTABLE)
        {
            SDK_WRAP_API(cfm, cfm_tx, pkt->cfm_info.lmep_hw_id, 1, pkt);
        }
        else
        {
            // Return false if destination is not reachable
            CFM_LOG(
                DN_LOG_ERR,
                "Failed to resolve MAC address %02X:%02X:%02X:%02X:%02X:%02X "
                "for LTM packet forwarding. Resolution: %d",
                ltm_hdr->target_mac.addr_bytes[0],
                ltm_hdr->target_mac.addr_bytes[1],
                ltm_hdr->target_mac.addr_bytes[2],
                ltm_hdr->target_mac.addr_bytes[3],
                ltm_hdr->target_mac.addr_bytes[4],
                ltm_hdr->target_mac.addr_bytes[5],
                resolution);
            return false;
        }
    }

    // Return true if packet was sent
    return true;
}

// send LTM packet on a specific direction, for a specific lmep_hw_id
bool LtmPacket::Send(int direction, int lmep_hw_id) const
{
    SDK_WRAP_API(cfm, cfm_tx, lmep_hw_id, direction, pkt);

    return true;
}

LtrPacket::LtrPacket(const LtmPacket& ltm,
                     uint16_t direction,
                     const ether_addr* my_cfm_mac)
{
    memcpy(pkt->data, ltm.GetWbPacket()->data, ltm.l2_size);
    pkt->data_len = ltm.l2_size;

    ParseL2Header();

    ltr_hdr = reinterpret_cast<LtrHeader*>(pkt->data + l2_size);
    pkt->data_len += sizeof(LtrHeader);

    ltr_hdr->cfm_hdr.md_info = ltm.ltm_hdr->cfm_hdr.md_info;
    ltr_hdr->cfm_hdr.opcode = OAM_OPCODE_LTR;
    ltr_hdr->cfm_hdr.first_tlv_offset = LTR_FIRST_TLV_OFFSET;
    ltr_hdr->cfm_hdr.flags = 0;

    ether_addr_copy(my_cfm_mac, &ether_hdr->s_addr);
    ether_addr_copy(&ltm.ltm_hdr->original_mac, &ether_hdr->d_addr);

    ltr_hdr->transaction_id = ltm.ltm_hdr->transaction_id;
    ltr_hdr->ttl = ltm.ltm_hdr->ttl - 1;

    if (!ltm.is_legacy_ltm)
    {
        ltr_egress_identifier_tlv = reinterpret_cast<LtrEgressIdentifierTlv*>(
            pkt->data + pkt->data_len);
        pkt->data_len += sizeof(LtrEgressIdentifierTlv);

        ltr_egress_identifier_tlv->type = LTR_EGRESS_IDENTIFIER_TLV_TYPE;
        ltr_egress_identifier_tlv->length =
            ntohs(LTR_EGRESS_IDENTIFIER_TLV_LENGTH);
        ltr_egress_identifier_tlv->last_egress_id =
            ltm.ltm_egress_identifier_tlv->initiator_id;
        ether_addr_copy(&ltm.ltm_egress_identifier_tlv->initiator_mac,
                        &ltr_egress_identifier_tlv->last_egress_mac);
        ltr_egress_identifier_tlv->next_egress_id = 0;
        ether_addr_copy(my_cfm_mac,
                        &ltr_egress_identifier_tlv->next_egress_mac);

        if (direction)
        {
            ltr_reply_egress_tlv =
                reinterpret_cast<LtrReplyEgressTlv*>(pkt->data + pkt->data_len);
            pkt->data_len += sizeof(LtrReplyEgressTlv);
            ltr_reply_egress_tlv->type = LTR_REPLY_EGRESS_TLV_TYPE;
            ltr_reply_egress_tlv->length = ntohs(LTR_REPLY_EGRESS_TLV_LENGTH);
            ltr_reply_egress_tlv->egress_action = ReplyAction::OK;
            ether_addr_copy(my_cfm_mac, &ltr_reply_egress_tlv->egress_mac);
        }
        else
        {
            ltr_reply_ingress_tlv = reinterpret_cast<LtrReplyIngressTlv*>(
                pkt->data + pkt->data_len);
            pkt->data_len += sizeof(LtrReplyIngressTlv);
            ltr_reply_ingress_tlv->type = LTR_REPLY_INGRESS_TLV_TYPE;
            ltr_reply_ingress_tlv->length = ntohs(LTR_REPLY_INGRESS_TLV_LENGTH);
            ltr_reply_ingress_tlv->ingress_action = ReplyAction::OK;
            ether_addr_copy(my_cfm_mac, &ltr_reply_ingress_tlv->ingress_mac);
        }
    }
    else
    {
        // Backward compatibility mode with Ver 2006 (ITU-T Y.1731)
        // Do not send TLVs, copy flags from LTM
        ltr_hdr->cfm_hdr.flags = ltm.ltm_hdr->cfm_hdr.flags;
    }

    if (!ltm.additional_tlv.empty())
    {
        // Copy additional TLVs from LTM
        memcpy(pkt->data + pkt->data_len,
               ltm.additional_tlv.data(),
               ltm.additional_tlv.size());
        pkt->data_len += ltm.additional_tlv.size();
    }

    pkt->data[pkt->data_len] = 0;
    ++pkt->data_len;
}

LtrPacket::LtrPacket(wb_pkt* wrap_pkt)
{
    memcpy(pkt->data, wrap_pkt->data, wrap_pkt->data_len);
    pkt->data_len = wrap_pkt->data_len;

    ParseL2Header();

    ltr_hdr = reinterpret_cast<LtrHeader*>(pkt->data + l2_size);
    pkt->data_len += sizeof(LtrHeader);

    if (unlikely(ltr_hdr->cfm_hdr.opcode != OAM_OPCODE_LTR))
    {
        throw std::runtime_error("Failed to find LTR frame in packet!");
    }

    uint16_t next_tlv_offset =
        l2_size + CFM_TLV_POS + ltr_hdr->cfm_hdr.first_tlv_offset;

    do
    {
        uint16_t tlv_length = 0;
        memcpy(
            &tlv_length, pkt->data + next_tlv_offset + 1, sizeof(tlv_length));

        if (pkt->data[next_tlv_offset] == LTR_EGRESS_IDENTIFIER_TLV_TYPE)
        {
            ltr_egress_identifier_tlv =
                reinterpret_cast<LtrEgressIdentifierTlv*>(pkt->data
                                                          + next_tlv_offset);
        }
        else if (pkt->data[next_tlv_offset] == LTR_REPLY_INGRESS_TLV_TYPE)
        {
            ltr_reply_ingress_tlv = reinterpret_cast<LtrReplyIngressTlv*>(
                pkt->data + next_tlv_offset);
        }
        else if (pkt->data[next_tlv_offset] == LTR_REPLY_EGRESS_TLV_TYPE)
        {
            ltr_reply_egress_tlv = reinterpret_cast<LtrReplyEgressTlv*>(
                pkt->data + next_tlv_offset);
        }

        next_tlv_offset += TLV_HEADER_SIZE + ntohs(tlv_length);

    } while (pkt->data[next_tlv_offset] != 0);
}

LtrPacket::~LtrPacket()
{
    if (!is_sent)
    {
        packet_pool_free(&pkt, 1);
    }
}

void LtrPacket::SetFlags(bool use_fdb_only, bool fwd_yes, bool terminal_mep)
{
    if (ltr_egress_identifier_tlv)
    {
        ltr_hdr->cfm_hdr.flags = 0;
        ltr_hdr->cfm_hdr.flags |= use_fdb_only << 7;
        ltr_hdr->cfm_hdr.flags |= fwd_yes << 6;
        ltr_hdr->cfm_hdr.flags |= terminal_mep << 5;
    }

    ltr_hdr->relay_action = terminal_mep ? RlyHit : RlyFDB;
}

void LtrPacket::Send(stw_t* timer_wheel)
{
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> distr(0, 1000);
    int wait_delay = distr(gen);

    // Send LTR after a random delay
    stw_tmr_t* delay_send_timer = new stw_tmr_t;
    stw_timer_prepare(delay_send_timer);
    int rc = stw_timer_start(timer_wheel,
                             delay_send_timer,
                             wait_delay,
                             0,
                             LtrPacket::SendDelayLtrCb,
                             pkt);
    is_sent = true;

    if (RC_STW_OK != rc)
    {
        SDK_WRAP_API(cfm,
                     cfm_tx,
                     pkt->cfm_info.lmep_hw_id,
                     pkt->cfm_info.direction,
                     pkt);
        delete delay_send_timer;
    }
}

void LtrPacket::SendDelayLtrCb(stw_tmr_t* delay_send_timer, void* ltm_wb_pkt)
{
    wb_pkt* pkt = static_cast<wb_pkt*>(ltm_wb_pkt);
    SDK_WRAP_API(
        cfm, cfm_tx, pkt->cfm_info.lmep_hw_id, pkt->cfm_info.direction, pkt);
    delete delay_send_timer;
}

SlmPacket::SlmPacket(const ether_addr* my_mac,
                     const ether_addr* dst_mac,
                     int inner_vlan,
                     int outer_vlan,
                     int pcp,
                     uint8_t level,
                     uint16_t source_mep,
                     uint32_t test_id,
                     uint32_t my_tx,
                     uint16_t inner_tpid,
                     uint16_t outer_tpid)
    : CfmPacket()
{
    ConfigureL2Header(
        my_mac, dst_mac, inner_vlan, outer_vlan, pcp, inner_tpid, outer_tpid);
    ConfigureSLMHeader(level, source_mep, test_id, my_tx);
}

void SlmPacket::ConfigureSLMHeader(uint8_t level,
                                   uint16_t source_mep,
                                   uint32_t test_id,
                                   uint32_t my_tx)
{
    slm_hdr = reinterpret_cast<SlmHeader*>(pkt->data + l2_size);

    slm_hdr->cfm_hdr.md_info = level << 5;
    slm_hdr->cfm_hdr.flags = 0;
    slm_hdr->cfm_hdr.first_tlv_offset = 16;
    slm_hdr->cfm_hdr.opcode = OAM_OPCODE_SLM;

    slm_hdr->src_mep_id = rte_cpu_to_be_16(source_mep);
    slm_hdr->test_id = rte_cpu_to_be_32(test_id);
    slm_hdr->my_tx = rte_cpu_to_be_32(my_tx);
    slm_hdr->res_dest_mep_id = 0;
    slm_hdr->res_remote_tx = 0;

    pkt->data_len += sizeof(SlmHeader);

    pkt->data[pkt->data_len] = 0;
    ++pkt->data_len;
}

int SlmPacket::Send(int direction, int lmep_hw_id)
{
    return SDK_WRAP_API(cfm, cfm_tx, lmep_hw_id, direction, this->pkt);
}

LoopbackPayload::LoopbackPayload(wb_pkt* pkt,
                                 const uint8_t opcode,
                                 const uint8_t l2_size)
{
    pkt->data += l2_size;
    lbm_hdr = *reinterpret_cast<LbmHeader*>(pkt->data);

    if (unlikely(lbm_hdr.cfm_hdr.opcode != opcode))
    {
        std::string log = fmt::format(
            "Failed to find Loopback {} frame in packet!",
            opcode == OAM_OPCODE_LBM ? "Message (LBM)" : "Reply (LBR)");
        throw std::runtime_error(log);
    }
    pkt->data += sizeof(LbmHeader);
    pkt->data_len -= sizeof(LbmHeader);

    // traverse TLV list. Only supported TLV is Data TLV
    if (pkt->data[0] == DATA_TLV_TYPE)
    {
        has_data_tlv = true;
        memcpy(&data_tlv, pkt->data, sizeof(DataTlv));
        pkt->data += sizeof(DataTlv);
        pkt->data_len -= sizeof(DataTlv);

        data_tlv_length = ntohs(data_tlv.length);

        if (data_tlv_length > 0) // account for data_tlv_length == 0
        {
            data_tlv_payload = new uint8_t[data_tlv_length];
            memcpy(data_tlv_payload, pkt->data, data_tlv_length);

            pkt->data += data_tlv_length;
            pkt->data_len -= data_tlv_length;
        }
    }

    end_tlv = pkt->data[0];

    std::string log =
        fmt::format("Loopback {} packet received with transaction ID {}",
                    lbm_hdr.cfm_hdr.opcode == OAM_OPCODE_LBM ? "Message (LBM)"
                                                             : "Reply (LBR)",
                    ntohl(lbm_hdr.transaction_id));

    if (has_data_tlv)
    {
        LOG_CFM_EXTRA_INFO(
            "%s, data_tlv type %d, data_tlv length %d, end_tlv %d",
            log.c_str(),
            data_tlv.type,
            data_tlv_length,
            end_tlv);
    }
    else
    {
        data_tlv = {0, 0};
        LOG_CFM_EXTRA_INFO("%s, end_tlv %d", log.c_str(), end_tlv);
    }
}

LoopbackPayload::LoopbackPayload(uint8_t level,
                                 uint32_t transaction_id,
                                 const uint16_t length)
{
    uint16_t payload_size =
        sizeof(LbmHeader)
        + sizeof(end_tlv); // this is actually LBM_MIN_PDU_SIZE

    lbm_hdr.cfm_hdr.md_info = level << 5;
    lbm_hdr.cfm_hdr.flags = 0;
    lbm_hdr.cfm_hdr.first_tlv_offset = LBM_FIRST_TLV_OFFSET;
    lbm_hdr.cfm_hdr.opcode = OAM_OPCODE_LBM;

    lbm_hdr.transaction_id = htonl(transaction_id);

    if (length >= LBM_MIN_PDU_SIZE_WITH_DATA_TLV)
    {
        has_data_tlv = true;
        data_tlv_length = length - LBM_MIN_PDU_SIZE_WITH_DATA_TLV;
        data_tlv.type = DATA_TLV_TYPE;
        data_tlv.length = htons(data_tlv_length);

        payload_size += sizeof(DataTlv);
        if (data_tlv_length > 0) // account for data_tlv_length == 0
        {
            data_tlv_payload = new uint8_t[data_tlv_length];
            memset(data_tlv_payload, 0, data_tlv_length);

            payload_size += data_tlv_length;
        }
    }

    std::string log =
        fmt::format("Loopback {} packet created with transaction_id {}",
                    lbm_hdr.cfm_hdr.opcode == OAM_OPCODE_LBM ? "Message (LBM)"
                                                             : "Reply (LBR)",
                    transaction_id);

    if (has_data_tlv)
    {
        LOG_CFM_EXTRA_INFO(
            "%s, data_tlv type %d, data_tlv length %d, end_tlv %d",
            log.c_str(),
            data_tlv.type,
            data_tlv_length,
            end_tlv);
    }
    else
    {
        LOG_CFM_EXTRA_INFO("%s, end_tlv %d", log.c_str(), end_tlv);
    }
}

void LoopbackPayload::FillPacket(wb_pkt* pkt, const uint8_t l2_size)
{
    memcpy(pkt->data + l2_size, &lbm_hdr, sizeof(LbmHeader));
    pkt->data_len += sizeof(LbmHeader);

    if (has_data_tlv)
    {
        memcpy(pkt->data + pkt->data_len, &data_tlv, sizeof(DataTlv));

        pkt->data_len += sizeof(DataTlv);

        if (data_tlv_length > 0) // account for data_tlv_length == 0
        {
            memcpy(
                pkt->data + pkt->data_len, data_tlv_payload, data_tlv_length);
            pkt->data_len += data_tlv_length;
        }
    }
    pkt->data[pkt->data_len] = END_TLV;
    ++pkt->data_len;
}

LoopbackPayload::~LoopbackPayload() { delete[] data_tlv_payload; }

bool LoopbackPayload::operator==(const LoopbackPayload& other) const
{
    bool ret = true;

    LOG_CFM_EXTRA_INFO("Comparing LBM and LBR packets:");

    LOG_CFM_EXTRA_INFO(
        "LBM: opcode %d, transaction_id %d, data_tlv type %d, data_tlv length "
        "%d, end_tlv %d",
        lbm_hdr.cfm_hdr.opcode,
        ntohl(lbm_hdr.transaction_id),
        data_tlv.type,
        data_tlv_length,
        end_tlv);

    LOG_CFM_EXTRA_INFO(
        "LBR: opcode %d, transaction_id %d, data_tlv type %d, data_tlv length "
        "%d, end_tlv %d",
        other.lbm_hdr.cfm_hdr.opcode,
        ntohl(other.lbm_hdr.transaction_id),
        other.data_tlv.type,
        other.data_tlv_length,
        other.end_tlv);

    // when performing MSDU checks, we don't look at the opcode and the transaction ID
    if (!has_data_tlv)
    {
        if (other.has_data_tlv)
        {
            CFM_LOG(DN_LOG_WARNING, "LBM has no Data TLV, but LBR has");
            return false;
        }
        if (end_tlv != other.end_tlv)
        {
            CFM_LOG(DN_LOG_WARNING,
                    "End TLV mismatch: %d != %d",
                    end_tlv,
                    other.end_tlv);
            return false;
        }
        return true;
    }
    if (data_tlv.type != other.data_tlv.type)
    {
        CFM_LOG(DN_LOG_WARNING,
                "Data TLV type mismatch: %d != %d",
                data_tlv.type,
                other.data_tlv.type);
        return false;
    }
    if (end_tlv != other.end_tlv)
    {
        CFM_LOG(DN_LOG_WARNING,
                "End TLV mismatch: %d != %d",
                end_tlv,
                other.end_tlv);
        return false;
    }
    if ((data_tlv_length == 0) && (other.data_tlv_length == 0))
    {
        CFM_LOG(DN_LOG_DEBUG,
                "DEBUG: Data TLV length is 0, skipping payload comparison");
        return true;
    }
    if (data_tlv_length != other.data_tlv_length)
    {
        CFM_LOG(DN_LOG_WARNING,
                "Data TLV length mismatch: %d != %d",
                data_tlv_length,
                other.data_tlv_length);
        ret = false;
    }
    if ((other.data_tlv_payload == nullptr) && (data_tlv_payload != nullptr))
    {
        CFM_LOG(DN_LOG_WARNING, "LBR: Data TLV payload is missing");
        return false;
    }

    if (memcmp(data_tlv_payload, other.data_tlv_payload, data_tlv_length) != 0)
    {
        CFM_LOG(DN_LOG_WARNING, "Data TLV payload mismatch");

        FMT_LOG_CFM_PACKET(
            "LBM Data TLV payload: ", data_tlv_payload, data_tlv_length);
        FMT_LOG_CFM_PACKET("LBR Data TLV payload: ",
                           other.data_tlv_payload,
                           other.data_tlv_length);

        ret = false;
    }
    return ret;
}

LbmPacket::LbmPacket(wb_pkt* wrap_pkt) : CfmPacket(wrap_pkt)
{
    ParseL2Header();
    lbm_payload =
        std::make_unique<LoopbackPayload>(wrap_pkt, OAM_OPCODE_LBM, l2_size);
}

LbmPacket::LbmPacket(const ether_addr* my_mac,
                     const ether_addr* dst_mac,
                     int inner_vlan,
                     int outer_vlan,
                     int pcp,
                     uint8_t level,
                     uint32_t transaction_id,
                     const uint16_t length,
                     uint16_t inner_tpid,
                     uint16_t outer_tpid)
    : CfmPacket(), lbm_payload(std::make_unique<LoopbackPayload>(
                       level, transaction_id, length))
{
    ConfigureL2Header(
        my_mac, dst_mac, inner_vlan, outer_vlan, pcp, inner_tpid, outer_tpid);

    lbm_payload->FillPacket(pkt, l2_size);
}

bool LbmPacket::Send(int direction, int lmep_hw_id)
{
    int rc = SDK_WRAP_API(cfm, cfm_tx, lmep_hw_id, direction, pkt);
    return not rc;
}

LbrPacket::LbrPacket(wb_pkt* wrap_pkt) : CfmPacket(wrap_pkt)
{
    ParseL2Header();

    lbm_payload =
        std::make_unique<LoopbackPayload>(pkt, OAM_OPCODE_LBR, l2_size);

    transaction_id = ntohl(lbm_payload->lbm_hdr.transaction_id);
}

LbrPacket::~LbrPacket() {}

} // namespace cfm
