#include "CfmSessionLTM.hpp"

#include <spdlog/fmt/fmt.h>

#include <vector>

#include "CfmManager.hpp"
#include "CfmPackets.hpp"
#include "CfmSessionStats.hpp"
#include "corm_api.h"
#include "sdk_wrap/sdk_common/wb_packet.h"

using std::lock_guard;

namespace cfm {

static uint32_t current_transaction_id = 1;

CfmSessionLTM::CfmSessionLTM(uint64_t sess_id,
                             stw_t* m_timer_wheel,
                             const SessionStartRequest& req,
                             const LMepOper& lmep_op)
    : CfmSession(sess_id, m_timer_wheel, req, lmep_op),
      transaction_id(current_transaction_id++), max_hops(req.max_hops)
{
    local_stats.transaction_id = transaction_id;
    if (current_transaction_id == 0) current_transaction_id = 1;
}

void CfmSessionLTM::SendPacket()
{
    const uint8_t cls = 0x38 | (cfg.level & 0x07); // use multicast DA Class 2
    const ether_addr mcast_class_2_dst_mac = {
        0x01, 0x80, 0xc2, 0x00, 0x00, cls};

    try
    {
        LtmPacket ltm(&cfg.src_mac,
                      &mcast_class_2_dst_mac,
                      cfg.inner_tag,
                      cfg.outer_tag,
                      cfg.pcp,
                      cfg.level,
                      transaction_id,
                      max_hops,
                      &cfg.dst_mac,
                      cfg.inner_tpid,
                      cfg.outer_tpid);

        bool ret = ltm.Send(MepDirection::UP == cfg.direction, cfg.hw_id);

        if (ret)
        {
            CfmManager::GetInstance()
                .GetOperation()
                .data.Meps.at(cfg.oam_id)
                .stats->ltm_out++;

            lock_guard _(m_lock);
            local_stats.my_tx++;

            tx_done = true;
        }
    }
    catch (const std::exception& e)
    {
        CFM_LOG(DN_LOG_ERROR, "INITIATOR LTM send packet failed %s", e.what());
    }
}

void CfmSessionLTM::HandlePacket(wb_pkt* pkt)
{
    LtrPacket ltr(pkt);

    char buffer[MAC_ADDRESS_LEN];

    const auto ltr_ttl = ltr.ltr_hdr->ttl;

    CfmManager::GetInstance()
        .GetOperation()
        .data.Meps.at(cfg.oam_id)
        .stats->ltr_in++;

    lock_guard _(m_lock);
    snprintf(buffer,
             MAC_ADDRESS_LEN,
             "%s",
             ltr.ltr_hdr->relay_action == RlyHit ? "RlyHit" : "RlyFDB");
    local_stats.hops[ltr_ttl].relay_action = buffer;

    ether_format_addr(buffer, MAC_ADDRESS_LEN, &ltr.ether_hdr->s_addr);
    local_stats.hops[ltr_ttl].src_mac_address = buffer;

    CFM_LOG(DN_LOG_INFO,
            "Initiator LTM handle packet with ttl %d from %s",
            ltr_ttl,
            buffer);

    if (ltr.ltr_egress_identifier_tlv != nullptr)
    {
        ether_format_addr(buffer,
                          MAC_ADDRESS_LEN,
                          &ltr.ltr_egress_identifier_tlv->last_egress_mac);
        local_stats.hops[ltr_ttl].last_egress_mac_address = buffer;

        ether_format_addr(buffer,
                          MAC_ADDRESS_LEN,
                          &ltr.ltr_egress_identifier_tlv->next_egress_mac);
        local_stats.hops[ltr_ttl].next_egress_mac_address = buffer;

        snprintf(buffer,
                 MAC_ADDRESS_LEN,
                 "%d",
                 ltr.ltr_egress_identifier_tlv->last_egress_id);
        local_stats.hops[ltr_ttl].last_egress_id = buffer;

        snprintf(buffer,
                 MAC_ADDRESS_LEN,
                 "%d",
                 ltr.ltr_egress_identifier_tlv->next_egress_id);
        local_stats.hops[ltr_ttl].next_egress_id = buffer;
    }

    if (ltr.ltr_reply_egress_tlv != nullptr)
    {
        FillReply(ltr_ttl,
                  ltr.ltr_reply_egress_tlv->egress_action,
                  &ltr.ltr_reply_egress_tlv->egress_mac,
                  0);
    }
    else if (ltr.ltr_reply_ingress_tlv != nullptr)
    {
        FillReply(ltr_ttl,
                  ltr.ltr_reply_ingress_tlv->ingress_action,
                  &ltr.ltr_reply_ingress_tlv->ingress_mac,
                  1);
    }

    local_stats.my_rx++;
}

void CfmSessionLTM::FillReply(uint8_t ttl,
                              uint8_t action,
                              ether_addr* mac_address,
                              uint8_t ingress)
{
    char buffer[MAC_ADDRESS_LEN];

    ether_format_addr(buffer, MAC_ADDRESS_LEN, mac_address);
    local_stats.hops[ttl].reply_mac_address = buffer;

    int len = snprintf(buffer, MAC_ADDRESS_LEN, ingress ? "Ing" : "Egr");

    switch (action)
    {
    case ReplyAction::OK:
        snprintf(buffer + len, MAC_ADDRESS_LEN - len, "OK");
        break;
    case ReplyAction::Blocked:
        snprintf(buffer + len, MAC_ADDRESS_LEN - len, "Blocked");
        break;
    case ReplyAction::Down:
        snprintf(buffer + len, MAC_ADDRESS_LEN - len, "Down");
        break;
    case ReplyAction::VID:
        snprintf(buffer + len, MAC_ADDRESS_LEN - len, "VID");
        break;
    default:
        snprintf(buffer, MAC_ADDRESS_LEN, "Unknown");
        break;
    }

    local_stats.hops[ttl].reply_action = buffer;
}

void CfmSessionLTM::TimerCb(stw_tmr_t* ptimer, void* pdata)
{
    CfmSessionLTM* sess = static_cast<CfmSessionLTM*>(pdata);

    if (sess->state == SESS_STATE_RUNNING)
    {
        sess->SendPacket();

        // start the time to wait for the packets
        constexpr uint32_t LTM_INITIATOR_WAIT_LAST_PACKET = 5000; // 5 seconds
        // session waits max 5s and then conclude the session and fill the stats
        stw_timer_start(sess->m_timer_wheel,
                        ptimer,
                        LTM_INITIATOR_WAIT_LAST_PACKET,
                        0,
                        CfmSessionLTM::TimerCb,
                        sess);
        sess->state = SESS_STATE_WAIT_LAST_PACKET;
    }
    else if (sess->state == SESS_STATE_WAIT_LAST_PACKET)
    {
        stw_timer_stop(sess->m_timer_wheel, ptimer);
        sess->state = SESS_STATE_DONE;
    }
}

SessionStartStatus CfmSessionLTM::Start()
{
    auto& oper = CfmManager::GetInstance().GetOperation();

    auto lmep_oper_it = oper.data.Meps.find(cfg.oam_id);
    if (lmep_oper_it == oper.data.Meps.end())
    {
        CFM_LOG(DN_LOG_ERR, "Initiator LTM: Failed to find MEP %d", cfg.oam_id);
        state = SESS_STATE_ABORT;
        return SESS_START_ERR_MISSING_MEP;
    }

    // send the packet asap
    int rc = stw_timer_start(
        m_timer_wheel, &m_timer, 0, 0, CfmSessionLTM::TimerCb, this);

    if (RC_STW_OK != rc)
    {
        CFM_LOG(DN_LOG_ERR, "INITIATOR LTM start failed timer %d", rc);
        state = SESS_STATE_DONE;

        return SESS_START_ERR;
    }

    state = SESS_STATE_RUNNING;

    return SESS_START_OK;
}

SessionStopStatus CfmSessionLTM::Stop()
{
    stw_timer_stop(m_timer_wheel, &m_timer);
    if (state != SESS_STATE_DONE)
        OperTestresultInvalid(SessionOpcode::SESS_LTM);

    state = SESS_STATE_ABORT;

    return SESS_STOP_OK;
}

static void saveOperResult(uint64_t,
                           SessionState,
                           const CfmSessionStatsLTM&,
                           const LocalMepData&) noexcept;

void CfmSessionLTM::PushOperResults()
{
    const auto& lmeps = CfmManager::GetInstance().GetOperation().data.Meps;

    auto lmep_it = lmeps.find(cfg.oam_id);
    if (lmeps.end() == lmep_it) return;

    lock_guard _(m_lock);

    saveOperResult(sess_id, state, local_stats, lmep_it->second.config);

    CFM_LOG(DN_LOG_INFO,
            "Initiator LTM stats tx %d rx %d for transaction id %d",
            local_stats.my_tx,
            local_stats.my_rx,
            local_stats.transaction_id);
}

bool CfmSessionLTM::OperTestInfoInitial(corm_obj* cobj) const
{
    // FIELD__max_hops
    int rc = corm_u8_set(cobj, OD_FIELD_(LTM(Testinfo), max_hops), max_hops);
    return not rc;
}

void saveOperResult(uint64_t sess_id,
                    SessionState state,
                    const CfmSessionStatsLTM& stats,
                    const LocalMepData& lmep) noexcept
{
    const auto mep_id_str = fmt::format("{}", lmep.mep_id);
    std::array<const char*, 4> lmep_key = {
        lmep.md_id.c_str(),
        lmep.ma_id.c_str(),
        mep_id_str.c_str(),
        "", // used for hop-id key
    };

    CFM_LOG(DN_LOG_INFO,
            "LTM Testresults %ssession %lx - MD %s MA %s LMEP %d %lu hop(s)",
            SessionState::SESS_STATE_DONE == state ? "'valid' " : "",
            sess_id,
            lmep.md_id.c_str(),
            lmep.ma_id.c_str(),
            lmep.mep_id,
            stats.hops.size());

    std::vector<corm_obj*> objs;
    objs.reserve(32);

    auto obj_r = corm_obj_new(
        OD_CLASS_(LTM(Testresults)), lmep_key.data(), lmep_key.size() - 1);

    if (not obj_r)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate LTM Testinfo corm_obj for session id %lx",
                sess_id);

        return;
    }

    objs.push_back(obj_r);

    int rc = corm_uint32_set( // FIELD__ltr_received /*uint32_t*/
        obj_r,
        OD_FIELD_(LTM(Testresults), ltr_received),
        stats.my_rx);
    rc |= corm_uint32_set( // FIELD__transaction_id /*uint32_t*/
        obj_r,
        OD_FIELD_(LTM(Testresults), transaction_id),
        stats.transaction_id);

    if (SessionState::SESS_STATE_DONE == state)
    {
        rc |= corm_enum_set( // FIELD__measurement_validity
            obj_r,
            OD_FIELD_(LTM(Testresults), measurement_validity),
            OD_FIELD_(LTM(TestresultsMeasurementvalidityEnum), valid));

        auto obj_i = corm_obj_new(
            OD_CLASS_(LTM(Testinfo)), lmep_key.data(), lmep_key.size() - 1);
        if (obj_i)
        {
            objs.push_back(obj_i);

            const std::time_t tm = time(NULL);
            char tbuf[32];
            // ISO 8601
            strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

            // FIELD__end_time
            rc |= corm_string_set(
                obj_i, OD_FIELD_(LTM(Testinfo), end_time), tbuf);
        }
    }

    uint8_t hop_num = 0;
    for (const auto& [ttl, hop] : stats.hops)
    {
        const auto hop_num_str = fmt::format("{}", ++hop_num);
        lmep_key.back() = hop_num_str.c_str();

        auto obj_h = corm_obj_new(OD_CLASS_(LTM(TestresultsHopinfoItem)),
                                  lmep_key.data(),
                                  lmep_key.size());

        if (not obj_h)
        {
            CFM_LOG(DN_LOG_ERR,
                    "Failed to allocate LTM TestresultsHopinfoItem corm_obj "
                    "for session id %lx",
                    sess_id);
            rc = 1;
            break;
        }

        objs.push_back(obj_h);

        rc |= corm_u8_set( // FIELD__hop_number /*uint8_t*/
            obj_h,
            OD_FIELD_(LTM(TestresultsHopinfoItem), hop_number),
            hop_num);
        rc |= corm_u8_set( // FIELD__ttl /*uint8_t*/
            obj_h,
            OD_FIELD_(LTM(TestresultsHopinfoItem), ttl),
            ttl);
        rc |= corm_string_set( // FIELD__source_mac_address /*mac_address*/
            obj_h,
            OD_FIELD_(LTM(TestresultsHopinfoItem), source_mac_address),
            hop.src_mac_address.c_str());
        rc |= corm_string_set( // FIELD__next_hop_mac_address /*mac_address*/
            obj_h,
            OD_FIELD_(LTM(TestresultsHopinfoItem), next_hop_mac_address),
            hop.next_egress_mac_address.c_str());
        // FIELD__previous_hop_mac_address /*mac_address*/
        rc |= corm_string_set(
            obj_h,
            OD_FIELD_(LTM(TestresultsHopinfoItem), previous_hop_mac_address),
            hop.last_egress_mac_address.c_str());
        rc |= corm_string_set( // FIELD__ingress_action /*string*/
            obj_h,
            OD_FIELD_(LTM(TestresultsHopinfoItem), ingress_action),
            hop.reply_action.c_str());
        rc |= corm_string_set( // FIELD__relay_action /*string*/
            obj_h,
            OD_FIELD_(LTM(TestresultsHopinfoItem), relay_action),
            hop.relay_action.c_str());
        // TODO(lbuga) FIELD__TestresultsHopinfoItem__egress_action = 6, /*string*/
    }

    const int32_t n_objs = objs.size();
    if (rc)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed corm_set for session id %lx - %d objects",
                sess_id,
                n_objs);
        corm_obj_destroy_array(objs.data(), n_objs);
    }
    else if (auto sent = dbclient_set(
                 objs.data(), n_objs, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             sent != n_objs)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed dbclient_set for session id %lx - %d objects",
                sess_id,
                n_objs);
    }
}

} // namespace cfm
