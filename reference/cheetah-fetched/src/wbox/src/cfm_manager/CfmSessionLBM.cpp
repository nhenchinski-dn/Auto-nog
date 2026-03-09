#include "CfmSessionLBM.hpp"

#include <spdlog/fmt/fmt.h>

#include "CfmManager.hpp"
#include "CfmTypes.hpp"
#include "corm_api.h"


using std::lock_guard;

namespace cfm {

CfmSessionLBM::CfmSessionLBM(uint64_t sess_id,
                             stw_t* m_timer_wheel,
                             const SessionStartRequest& req,
                             const LMepOper& lmep_op)
    : CfmSession(sess_id, m_timer_wheel, req, lmep_op)
{
}

CfmSessionLBM::~CfmSessionLBM() { m_transaction_timers.clear(); }

void CfmSessionLBM::TimerCb(stw_tmr_t* ptimer, void* pdata)
{
    auto sess = static_cast<CfmSessionLBM*>(pdata);

    if (sess->state == SESS_STATE_RUNNING)
    {
        sess->SendPacket();
        if (sess->local_stats.tx_count == sess->cfg.pkt_count)
        {
            sess->state = SESS_STATE_WAIT_LAST_PACKET;

            stw_timer_start(sess->m_timer_wheel,
                            ptimer,
                            CFM_INITIATOR_WAIT_LAST_PACKET,
                            0,
                            CfmSessionLBM::TimerCb,
                            sess);
        }
    }
    else if (sess->state == SESS_STATE_WAIT_LAST_PACKET)
    {
        stw_timer_stop(sess->m_timer_wheel, ptimer);
        sess->state = SESS_STATE_DONE;
    }
}

SessionStartStatus CfmSessionLBM::Start()
{
    auto& oper = CfmManager::GetInstance().GetOperation();

    auto lmep_oper_it = oper.data.Meps.find(cfg.oam_id);
    if (lmep_oper_it == oper.data.Meps.end())
    {
        CFM_LOG(DN_LOG_ERR, "Initiator LBM: Failed to find MEP %d", cfg.oam_id);
        state = SESS_STATE_ABORT;
        return SESS_START_ERR_MISSING_MEP;
    }

    int rc = stw_timer_start(m_timer_wheel,
                             &m_timer,
                             0,
                             cfg.pkt_interval_ms,
                             CfmSessionLBM::TimerCb,
                             this);

    if (RC_STW_OK != rc)
    {
        CFM_LOG(DN_LOG_ERR, "INITIATOR LBM: Failed to start timer: %d", rc);
        state = SESS_STATE_DONE;

        return SESS_START_ERR;
    }

    state = SESS_STATE_RUNNING;

    return SESS_START_OK;
}

SessionStopStatus CfmSessionLBM::Stop()
{
    stw_timer_stop(m_timer_wheel, &m_timer);
    if (state != SESS_STATE_DONE)
        OperTestresultInvalid(SessionOpcode::SESS_LBM);

    state = SESS_STATE_ABORT;

    return SESS_STOP_OK;
}

static void saveOperResult(uint64_t,
                           SessionState,
                           const CfmSessionStatsLBM&,
                           const LocalMepData&) noexcept;

void CfmSessionLBM::PushOperResults()
{
    const auto& lmeps = CfmManager::GetInstance().GetOperation().data.Meps;

    auto lmep_it = lmeps.find(cfg.oam_id);
    if (lmeps.end() == lmep_it) return;

    lock_guard _(m_lock);

    saveOperResult(sess_id, state, local_stats, lmep_it->second.config);
    LOG_CFM_EXTRA_INFO(
        "INITIATOR LBM: stats tx %d rx %d good_packets %d bad_order %d "
        "malformed %d timeout %d (internal: %d anomalies)",
        local_stats.tx_count,
        local_stats.rx_count,
        local_stats.rx_good_count,
        local_stats.rx_bad_order_count,
        local_stats.rx_malformed_count,
        local_stats.rx_timeout_count,
        local_stats.rx_anomaly_count);
}

void CfmSessionLBM::SendPacket()
{
    try
    {
        LbmPacket lbmPacket(&cfg.src_mac,
                            &cfg.dst_mac,
                            cfg.inner_tag,
                            cfg.outer_tag,
                            cfg.pcp,
                            cfg.level,
                            transaction_id,
                            cfg.pkt_size,
                            cfg.inner_tpid,
                            cfg.outer_tpid);
        bool ret = lbmPacket.Send(MepDirection::UP == cfg.direction, cfg.hw_id);

        if (ret)
        {
            AddTransaction(transaction_id, lbmPacket.ReleaseLbmPayload());

            CfmManager::GetInstance()
                .GetOperation()
                .data.Meps.at(cfg.oam_id)
                .stats->lbm_out++;

            lock_guard _(m_lock);
            local_stats.tx_count++;
            transaction_id++;
        }
    }
    catch (const std::exception& e)
    {
        CFM_LOG(DN_LOG_ERR,
                "INITIATOR LBM: Failed to send packet: %s stopping session",
                e.what());
        // guard session against this -> stop it
        stw_timer_stop(m_timer_wheel, &m_timer);
    }
}

void CfmSessionLBM::HandlePacket(wb_pkt* pkt)
{
    LbrPacket lbrPacket(pkt);

    CfmManager::GetInstance()
        .GetOperation()
        .data.Meps.at(cfg.oam_id)
        .stats->lbr_in++;

    lock_guard _(m_lock);

    local_stats.rx_count++;
    uint32_t rx_transaction_id = lbrPacket.GetTransactionId();

    if (m_expired_transactions.contains(rx_transaction_id))
    {
        CFM_LOG(DN_LOG_WARNING,
                "INITIATOR LBM: Received LBR packet with transaction id %d "
                "after rx timeout expired",
                rx_transaction_id);
        // local_stats.rx_timeout_count already increased when the timer expired
        m_expired_transactions.erase(rx_transaction_id);
        return;
    }

    auto it = m_transaction_timers.find(rx_transaction_id);
    if (it != m_transaction_timers.end())
    {
        auto& transact = it->second;
        const auto msdu_match =
            transact->IsMsduMatch(std::move(lbrPacket.ReleaseLbmPayload()));

        // msdu match will not check the transaction id by design
        const auto order_ok = transact->IsGoodOrder(rx_transaction_id);

        local_stats.rx_good_count += (msdu_match and order_ok);
        local_stats.rx_bad_order_count += (not order_ok);
        local_stats.rx_malformed_count += (not msdu_match);

        m_transaction_timers.erase(it);
    }
    else
    {
        CFM_LOG(DN_LOG_WARNING,
                "INITIATOR LBM: Received unexpected LBR packet with "
                "transaction id %d (anomaly)",
                rx_transaction_id);
        local_stats.rx_anomaly_count++; // unexpected transaction_id
        local_stats.rx_count--;         // don't count this as a valid rx
    }
}

void CfmSessionLBM::AddTransaction(uint32_t transactionId,
                                   std::unique_ptr<LoopbackPayload> payload)
{
    auto callback = [this](uint32_t transactionId) {
        this->DeleteTransaction(transactionId);
    };
    m_transaction_timers[transactionId] =
        std::make_unique<CfmSessionLbmTransaction>(
            transactionId, m_timer_wheel, std::move(payload), callback);
}

void CfmSessionLBM::DeleteTransaction(uint32_t transactionId)
{
    auto it = m_transaction_timers.find(transactionId);
    if (it != m_transaction_timers.end())
    {
        lock_guard _(m_lock);

        m_transaction_timers.erase(it);
        local_stats.rx_timeout_count++;
        m_expired_transactions.insert(transactionId);
        CFM_LOG(DN_LOG_WARNING,
                "INITIATOR LBM: Transaction %d timed out, timeout_count %d",
                transactionId,
                local_stats.rx_timeout_count);
    }
}

CfmSessionLbmTransaction::CfmSessionLbmTransaction(
    uint32_t transaction_id,
    stw_t* timer_wheel,
    std::unique_ptr<LoopbackPayload> payload,
    TimerCallback callback)
    : transaction_id(transaction_id), m_timer_wheel(timer_wheel),
      timer_callback(callback)
{
    m_payload = std::move(payload);
    stw_timer_prepare(&transaction_timer);

    constexpr uint32_t LOOPBACK_RX_PACKET_TIMEOUT = 2000; // 2 seconds
    int rc = stw_timer_start(m_timer_wheel,
                             &transaction_timer,
                             LOOPBACK_RX_PACKET_TIMEOUT,
                             0,
                             CfmSessionLbmTransaction::RxTimerCb,
                             this);
    if (RC_STW_OK != rc)
    {
        throw std::runtime_error("Failed to start packet timer");
    }
}

CfmSessionLbmTransaction::~CfmSessionLbmTransaction()
{
    stw_timer_stop(m_timer_wheel, &transaction_timer);
}

void CfmSessionLbmTransaction::RxTimerCb(stw_tmr_t* ptimer, void* pdata)
{
    auto transaction = static_cast<CfmSessionLbmTransaction*>(pdata);
    stw_timer_stop(transaction->m_timer_wheel, ptimer);

    // Call the callback to handle the transaction deletion
    if (transaction->timer_callback)
    {
        transaction->timer_callback(transaction->transaction_id);
    }
}

inline bool CfmSessionLbmTransaction::IsGoodOrder(
    uint32_t rx_transaction_id) const
{
    return transaction_id == rx_transaction_id;
}

inline bool CfmSessionLbmTransaction::IsMsduMatch(
    const std::unique_ptr<LoopbackPayload>& lbr_packet) const
{
    return *m_payload == *lbr_packet;
}

bool CfmSessionLBM::OperTestInfoInitial(corm_obj* cobj) const
{
    // FIELD__count
    int rc = corm_u16_set(cobj, OD_FIELD_(LBM(Testinfo), count), cfg.pkt_count);
    // FIELD__size
    rc |= corm_u16_set(cobj, OD_FIELD_(LBM(Testinfo), size), cfg.pkt_size);
    // FIELD__interval
    rc |= corm_u16_set(
        cobj, OD_FIELD_(LBM(Testinfo), interval), cfg.pkt_interval_ms / 1000);

    return not rc;
}

void saveOperResult(uint64_t sess_id,
                    SessionState state,
                    const CfmSessionStatsLBM& stats,
                    const LocalMepData& lmep) noexcept
{
    const auto mep_id_str = fmt::format("{}", lmep.mep_id);
    std::array<const char*, 3> lmep_key = {
        lmep.md_id.c_str(),
        lmep.ma_id.c_str(),
        mep_id_str.c_str(),
    };

    CFM_LOG(DN_LOG_INFO,
            "LBM Testresults %s %lx session - MD %s MA %s LMEP %d",
            SessionState::SESS_STATE_DONE == state ? "'valid'" : "",
            sess_id,
            lmep.md_id.c_str(),
            lmep.ma_id.c_str(),
            lmep.mep_id);

    std::array<corm_obj*, 2> objs;

    auto obj_r = corm_obj_new(
        OD_CLASS_(LBM(Testresults)), lmep_key.data(), lmep_key.size());

    if (not obj_r)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate LBM Testinfo corm_obj for session id %lx",
                sess_id);

        return;
    }
    objs[0] = obj_r;
    int32_t n_objs = 1;

    int rc = corm_uint32_set( // FIELD__lbm_transmitted /*uint32_t*/
        obj_r,
        OD_FIELD_(LBM(Testresults), lbm_transmitted),
        stats.tx_count);
    rc |= corm_uint32_set( // FIELD__lbr_received /*uint32_t*/
        obj_r,
        OD_FIELD_(LBM(Testresults), lbr_received),
        stats.rx_count);
    rc |= corm_double64_set( // FIELD__success_rate_percent /*double64_t*/
        obj_r,
        OD_FIELD_(LBM(Testresults), success_rate_percent),
        stats.tx_count ? stats.rx_good_count * 100.0 / stats.tx_count : 0);
    rc |= corm_uint32_set( // FIELD__lbr_timeout = 4, /*uint32_t*/
        obj_r,
        OD_FIELD_(LBM(Testresults), lbr_timeout),
        stats.rx_timeout_count);
    rc |= corm_uint32_set( // FIELD__lost_lbr /*uint32_t*/
        obj_r,
        OD_FIELD_(LBM(Testresults), lost_lbr),
        stats.tx_count - stats.rx_count);
    rc |= corm_uint32_set( // FIELD__invalid_lbr /*uint32_t*/
        obj_r,
        OD_FIELD_(LBM(Testresults), invalid_lbr),
        stats.rx_malformed_count);
    rc |= corm_uint32_set( // FIELD__out_of_order_lbr /*uint32_t*/
        obj_r,
        OD_FIELD_(LBM(Testresults), out_of_order_lbr),
        stats.rx_bad_order_count);

    if (SessionState::SESS_STATE_DONE == state)
    {
        rc |= corm_enum_set( // FIELD__measurement_validity
            obj_r,
            OD_FIELD_(LBM(Testresults), measurement_validity),
            OD_FIELD_(LBM(TestresultsMeasurementvalidityEnum), valid));

        auto obj_i = corm_obj_new(
            OD_CLASS_(LBM(Testinfo)), lmep_key.data(), lmep_key.size());
        if (obj_i)
        {
            objs[n_objs++] = obj_i;

            const std::time_t tm = time(NULL);
            char tbuf[32];
            // ISO 8601
            strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

            // FIELD__end_time
            rc |= corm_string_set(
                obj_i, OD_FIELD_(LBM(Testinfo), end_time), tbuf);
        }
    }

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR, "Failed corm_set for session id %lx", sess_id);
        corm_obj_destroy_array(objs.data(), n_objs);
    }
    else if (auto sent = dbclient_set(
                 objs.data(), n_objs, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             n_objs != sent)
        CFM_LOG(DN_LOG_ERR, "Failed dbclient_set for session id %lx", sess_id);
}

} // namespace cfm
