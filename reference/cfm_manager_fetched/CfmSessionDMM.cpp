#include "CfmSessionDMM.hpp"

#include <spdlog/fmt/fmt.h>
#include <system_events/include/dn_events_system.h>

#include "CfmLocalMep.hpp"
#include "CfmManager.hpp"
#include "corm_api.h"
#include "dbclient_api.h"


namespace cfm {

void CfmSessionDMM::TimerCb(stw_tmr_t*, void* pdata)
{
    CfmSessionDMM* sess = static_cast<CfmSessionDMM*>(pdata);

    switch (sess->state)
    {
    case SESS_STATE_RUNNING:
        sess->Disable();
        break;
    case SESS_STATE_WAIT_LAST_PACKET:
        sess->Wait();
        break;
    default:
        break;
    }
}

CfmSessionDMM::CfmSessionDMM(uint64_t sess_id,
                             stw_t* m_timer_wheel,
                             const SessionStartRequest& req,
                             const LMepOper& lmep_op)
    : CfmSession(sess_id, m_timer_wheel, req, lmep_op)
{
    SDK_WRAP_API(cfm, cfm_get_mep_hw_id, req.oam_id, &hw_id);
}

bool CfmSessionDMM::IsSupported()
{
    return !!SDK_WRAP_API(cfm, cfm_eth_dm_supported);
}

static void proactiveResultInvalid(const Proactive&) noexcept;

SessionStartStatus CfmSessionDMM::Start()
{
    int rv = SDK_WRAP_API(cfm,
                          cfm_start_eth_dm_session,
                          hw_id,
                          cfg.dst_mac.addr_bytes,
                          cfg.pcp,
                          cfg.pkt_interval_ms);
    if (rv)
    {
        CFM_LOG(DN_LOG_ERR,
                "BCM SDK: Failed to create ETH-DM session for hw_id 0x%x",
                hw_id);

        if (cfg.proactive) proactiveResultInvalid(*cfg.proactive);

        state = SESS_STATE_ABORT;
        return SESS_START_ERR;
    }

    uint32_t session_disable_ts =
        cfg.pkt_interval_ms * (cfg.pkt_count - 1) + 500;

    rv = stw_timer_start(m_timer_wheel,
                         &m_timer,
                         session_disable_ts,
                         CFM_INITIATOR_WAIT_LAST_PACKET,
                         CfmSessionDMM::TimerCb,
                         this);
    if (RC_STW_OK != rv)
    {
        CFM_LOG(DN_LOG_ERR,
                "ETH-DM start session failed to arm timer for hw_id 0x%x; "
                "reason %d",
                hw_id,
                rv);
        Stop();
        return SESS_START_ERR;
    }

    ts_start = corm_utils_timestamp();
    state = SESS_STATE_RUNNING;
    return SESS_START_OK;
}

SessionStopStatus CfmSessionDMM::Stop()
{
    CFM_LOG(DN_LOG_DEBUG, "ETH-DM stop session for hw_id 0x%x", hw_id);

    stw_timer_stop(m_timer_wheel, &m_timer);

    if (state != SESS_STATE_DONE)
    {
        cfg.proactive ? proactiveResultInvalid(*cfg.proactive)
                      : OperTestresultInvalid(SessionOpcode::SESS_DMM);
    }

    state = SESS_STATE_ABORT;

    int rv = SDK_WRAP_API(cfm, cfm_delete_eth_dm_session, hw_id);
    if (rv)
    {
        CFM_LOG(DN_LOG_ERR,
                "BCM SDK: Failed to delete ETH-DM session for hw_id 0x%x",
                hw_id);
        return SESS_STOP_ERR;
    }

    return SESS_STOP_OK;
}

SessionStopStatus CfmSessionDMM::Disable()
{
    CFM_LOG(DN_LOG_DEBUG, "ETH-DM tx disable session for hw_id 0x%x", hw_id);

    state = SESS_STATE_WAIT_LAST_PACKET;
    if (SDK_WRAP_API(cfm, cfm_stop_eth_dm_session, hw_id))
    {
        CFM_LOG(DN_LOG_ERR,
                "BCM SDK: Failed to tx disable ETH-DM session for hw_id 0x%x",
                hw_id);
        return SESS_STOP_ERR;
    }

    return SESS_STOP_OK;
}

static inline void send_proactive_event(
    const Proactive& pa,
    const LocalMepData& lmep,
    const CfmSessionStatsDMM& stats) noexcept
{
    const auto& thr = std::get<Proactive::DmmThreshold>(*pa.event_threshold);

    uint32_t thr_type = 0;
    uint32_t val = UINT32_MAX;
    std::string thresh_str;
    std::string val_str;

    float succ_rate =
        stats.tx_count ? stats.rx_count * 100.0 / stats.tx_count : 0;

    const std::time_t tm = time(NULL);
    char tbuf[32];
    strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

    if (thr.delay_rtt_min && *thr.delay_rtt_min < stats.delay_min)
    {
        thr_type = 1;
        val = stats.delay_min;
        thresh_str = fmt::format("{}us", *thr.delay_rtt_min);
        val_str = fmt::format("{}us", stats.delay_min);
    }
    else if (thr.delay_rtt_avg && *thr.delay_rtt_avg < stats.delay_avg)
    {
        thr_type = 2;
        val = stats.delay_avg;
        thresh_str = fmt::format("{}us", *thr.delay_rtt_avg);
        val_str = fmt::format("{}us", stats.delay_avg);
    }
    else if (thr.delay_rtt_max && *thr.delay_rtt_max < stats.delay_max)
    {
        thr_type = 3;
        val = stats.delay_max;
        thresh_str = fmt::format("{}us", *thr.delay_rtt_max);
        val_str = fmt::format("{}us", stats.delay_max);
    }
    else if (thr.jitter_rtt_avg && *thr.jitter_rtt_avg < stats.delay_variation)
    {
        thr_type = 4;
        val = stats.delay_variation;
        thresh_str = fmt::format("{}us", *thr.jitter_rtt_avg);
        val_str = fmt::format("{}us", stats.delay_variation);
    }
    else if (thr.jitter_rtt_max
             && *thr.jitter_rtt_max < stats.delay_variation_max)
    {
        thr_type = 5;
        val = stats.delay_variation_max;
        thresh_str = fmt::format("{}us", *thr.jitter_rtt_max);
        val_str = fmt::format("{}us", stats.delay_variation_max);
    }
    else if (thr.success_rate_pcnt && *thr.success_rate_pcnt > succ_rate)
    {
        thr_type = 6;
        val = succ_rate * 100;
        thresh_str = fmt::format("{}%", *thr.success_rate_pcnt);
        val_str = fmt::format("{}%", succ_rate);
    }

    if (val < UINT32_MAX)
        send_cfm_proactive_test_failure(1, // DMM
                                        pa.name.c_str(),
                                        pa.result_index,
                                        lmep.mep_id,
                                        lmep.ma_id.c_str(),
                                        lmep.md_id.c_str(),
                                        lmep.md_level,
                                        thresh_str.c_str(),
                                        val_str.c_str(),
                                        tbuf,
                                        thr_type,
                                        val);
}

void CfmSessionDMM::Wait()
{
    CFM_LOG(DN_LOG_DEBUG, "ETH-DM wait stats read for hw_id 0x%x", hw_id);
    stw_timer_stop(m_timer_wheel, &m_timer);
    state = SESS_STATE_DONE;

    if (cfg.proactive && cfg.proactive->event_threshold)
    {
        const auto& lmeps = CfmManager::GetInstance().GetOperation().data.Meps;

        const auto lmep_it = lmeps.find(cfg.oam_id);
        if (lmeps.end() == lmep_it) return;

        const auto& lmep = lmep_it->second.config;

        const auto& pa = *cfg.proactive;
        send_proactive_event(pa, lmep, stats);
    }
}

static void saveOperResult(uint64_t,
                           SessionState,
                           const CfmSessionStatsDMM&,
                           const LocalMepData&) noexcept;
static void updateProactiveResult(SessionState,
                                  const Proactive&,
                                  const CfmSessionStatsDMM&) noexcept;

void CfmSessionDMM::PushOperResults()
{
    if (state == SESS_STATE_INITIALIZING) return;

    stats.tx_count = std::min(
        (corm_utils_timestamp() - ts_start) / (cfg.pkt_interval_ms) + 1,
        (uint64_t)cfg.pkt_count);

    cfm_eth_dm_stats_t dm_stats = {};

    int count = 10;
    while (count--)
    {
        int rv =
            SDK_WRAP_API(cfm, cfm_get_eth_dm_session_stats, hw_id, &dm_stats);

        if (rv == -ENOENT)
            break;
        else if (rv == -EINVAL)
            return;

        stats.rx_count = dm_stats.dmr_count;
        stats.last_delay = dm_stats.last_delay;
        stats.delay_min = dm_stats.delay_min;
        stats.delay_max = dm_stats.delay_max;
        stats.delay_avg =
            dm_stats.dmr_count ? dm_stats.delay_sum / dm_stats.dmr_count : 0;
        stats.delay_variation = dm_stats.delay_variation;
        stats.delay_variation_max = dm_stats.delay_variation_max;
        stats.jitter = dm_stats.dmr_count
                           ? dm_stats.delay_variation_sum / dm_stats.dmr_count
                           : 0;
    }

    const auto& lmeps = CfmManager::GetInstance().GetOperation().data.Meps;

    auto lmep_it = lmeps.find(cfg.oam_id);
    if (lmeps.end() != lmep_it)
    {
        cfg.proactive
            ? updateProactiveResult(state, *cfg.proactive, stats)
            : saveOperResult(sess_id, state, stats, lmep_it->second.config);
    }
}

bool CfmSessionDMM::OperTestInfoInitial(corm_obj* cobj) const
{
    // FIELD__count
    int rc = corm_u16_set(cobj, OD_FIELD_(DMM(Testinfo), count), cfg.pkt_count);
    // FIELD__interval
    rc |= corm_u16_set(
        cobj, OD_FIELD_(DMM(Testinfo), interval), cfg.pkt_interval_ms / 1000);

    return not rc;
}

void saveOperResult(uint64_t sess_id,
                    SessionState state,
                    const CfmSessionStatsDMM& stats,
                    const LocalMepData& lmep) noexcept
{
    // CLASS_TYPE__TwowaydelaymeasurementTestresults
    const auto mep_id_str = fmt::format("{}", lmep.mep_id);
    std::array<const char*, 3> lmep_key = {
        lmep.md_id.c_str(),
        lmep.ma_id.c_str(),
        mep_id_str.c_str(),
    };

    CFM_LOG(DN_LOG_INFO,
            "DMM Testresults %s %lx session - MD %s MA %s LMEP %d",
            SessionState::SESS_STATE_DONE == state ? "'valid'" : "",
            sess_id,
            lmep.md_id.c_str(),
            lmep.ma_id.c_str(),
            lmep.mep_id);

    std::array<corm_obj*, 4> objs;

    auto obj_r = corm_obj_new(
        OD_CLASS_(DMM(Testresults)), lmep_key.data(), lmep_key.size());

    if (not obj_r)
    {
        CFM_LOG(DN_LOG_ERR,
                "corm_obj_new DMM Testresults corm_obj for session id %lx",
                sess_id);

        return;
    }
    objs[0] = obj_r;
    int32_t n_objs = 1;

    int rc = corm_uint32_set( // FIELD__dmm_transmitted /*uint32_t*/
        obj_r,
        OD_FIELD_(DMM(Testresults), dmm_transmitted),
        stats.tx_count);
    rc |= corm_uint32_set( // FIELD__dmr_received /*uint32_t*/
        obj_r,
        OD_FIELD_(DMM(Testresults), dmr_received),
        stats.rx_count);
    rc |= corm_double64_set( // FIELD__success_rate_percent /*double64_t*/
        obj_r,
        OD_FIELD_(DMM(Testresults), success_rate_percent),
        stats.tx_count ? stats.rx_count * 100.0 / stats.tx_count : 0);

    if (SessionState::SESS_STATE_DONE == state)
    {
        rc |= corm_enum_set( // FIELD__measurement_validity
            obj_r,
            OD_FIELD_(DMM(Testresults), measurement_validity),
            OD_FIELD_(DMM(TestresultsMeasurementvalidityEnum), valid));

        auto obj_i = corm_obj_new(
            OD_CLASS_(DMM(Testinfo)), lmep_key.data(), lmep_key.size());
        if (obj_i)
        {
            objs[n_objs++] = obj_i;

            const std::time_t tm = time(NULL);
            char tbuf[32];
            // ISO 8601
            strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

            // FIELD__end_time
            rc |= corm_string_set(
                obj_i, OD_FIELD_(DMM(Testinfo), end_time), tbuf);
        }
    }

    auto obj_rtd = corm_obj_new(OD_CLASS_(DMM(TestresultsRoundtripdelay)),
                                lmep_key.data(),
                                lmep_key.size());
    if (obj_rtd)
    {
        objs[n_objs++] = obj_rtd;

        rc |= corm_uint32_set( // FIELD__frame_delay_two_way_min /*uint32_t*/
            obj_rtd,
            OD_FIELD_(DMM(TestresultsRoundtripdelay), frame_delay_two_way_min),
            stats.delay_min);
        rc |= corm_uint32_set( // FIELD__frame_delay_two_way_avg /*uint32_t*/
            obj_rtd,
            OD_FIELD_(DMM(TestresultsRoundtripdelay), frame_delay_two_way_avg),
            stats.delay_avg);
        rc |= corm_uint32_set( // FIELD__frame_delay_two_way_max /*uint32_t*/
            obj_rtd,
            OD_FIELD_(DMM(TestresultsRoundtripdelay), frame_delay_two_way_max),
            stats.delay_max);
        rc |= corm_uint32_set( // FIELD__last_delay /*uint32_t*/
            obj_rtd,
            OD_FIELD_(DMM(TestresultsRoundtripdelay), last_delay),
            stats.last_delay);
    }

    auto obj_dv = corm_obj_new(OD_CLASS_(DMM(TestresultsDelayvariation)),
                               lmep_key.data(),
                               lmep_key.size());
    if (obj_dv)
    {
        objs[n_objs++] = obj_dv;

        rc |= corm_uint32_set( // FIELD__ifdv_two_way_avg /*uint32_t*/
            obj_dv,
            OD_FIELD_(DMM(TestresultsDelayvariation), ifdv_two_way_avg),
            stats.jitter);
        rc |= corm_uint32_set( // FIELD__ifdv_two_way_max /*uint32_t*/
            obj_dv,
            OD_FIELD_(DMM(TestresultsDelayvariation), ifdv_two_way_max),
            stats.delay_variation_max);
        rc |= corm_uint32_set( // FIELD__last_jitter /*uint32_t*/
            obj_dv,
            OD_FIELD_(DMM(TestresultsDelayvariation), last_jitter),
            stats.jitter);
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

void updateProactiveResult(SessionState state,
                           const Proactive& proa,
                           const CfmSessionStatsDMM& stats) noexcept
{
    const auto idx = fmt::format("{}", proa.result_index);
    std::array<const char*, 2> key = {
        proa.name.c_str(),
        idx.c_str(),
    };

    auto cid = PA_CLASS_(P_DMM(MeasurementresultItem));
    auto obj_r = corm_obj_new(cid, key.data(), key.size());

    if (not obj_r)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate DMM Measurementresult corm_obj for "
                "session %s result index %d",
                proa.name.c_str(),
                proa.result_index);
        return;
    }

    std::array<corm_obj*, 3> objs;

    objs[0] = obj_r;
    int32_t n_objs = 1;

    int rc = corm_uint32_set( // FIELD__dmm_transmitted /*uint32_t*/
        obj_r,
        PA_FIELD_(P_DMM(MeasurementresultItem), dmm_transmitted),
        stats.tx_count);
    rc |= corm_uint32_set( // FIELD__dmr_received /*uint32_t*/
        obj_r,
        PA_FIELD_(P_DMM(MeasurementresultItem), dmr_received),
        stats.rx_count);
    rc |= corm_double64_set( // FIELD__success_rate_percent /*double64_t*/
        obj_r,
        PA_FIELD_(P_DMM(MeasurementresultItem), success_rate_percent),
        stats.tx_count ? stats.rx_count * 100.0 / stats.tx_count : 0);

    if (SessionState::SESS_STATE_DONE == state)
    {
        rc |= corm_enum_set( // FIELD__measurement_validity
            obj_r,
            PA_FIELD_(P_DMM(MeasurementresultItem), measurement_validity),
            PA_FIELD_(P_DMM(MeasurementresultMeasurementvalidityEnum), valid));

        const std::time_t tm = time(NULL);
        char tbuf[32];
        // ISO 8601
        strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

        // FIELD__end_time
        rc |= corm_string_set(
            obj_r, PA_FIELD_(P_DMM(MeasurementresultItem), end_time), tbuf);
    }

    auto obj_rtd =
        corm_obj_new(PA_CLASS_(P_DMM(MeasurementresultRoundtripdelay)),
                     key.data(),
                     key.size());
    if (obj_rtd)
    {
        objs[n_objs++] = obj_rtd;

        rc |= corm_uint32_set( // FIELD__frame_delay_two_way_min /*uint32_t*/
            obj_rtd,
            PA_FIELD_(P_DMM(MeasurementresultRoundtripdelay),
                      frame_delay_two_way_min),
            stats.delay_min);
        rc |= corm_uint32_set( // FIELD__frame_delay_two_way_avg /*uint32_t*/
            obj_rtd,
            PA_FIELD_(P_DMM(MeasurementresultRoundtripdelay),
                      frame_delay_two_way_avg),
            stats.delay_avg);
        rc |= corm_uint32_set( // FIELD__frame_delay_two_way_max /*uint32_t*/
            obj_rtd,
            PA_FIELD_(P_DMM(MeasurementresultRoundtripdelay),
                      frame_delay_two_way_max),
            stats.delay_max);
        rc |= corm_uint32_set( // FIELD__last_delay /*uint32_t*/
            obj_rtd,
            PA_FIELD_(P_DMM(MeasurementresultRoundtripdelay), last_delay),
            stats.last_delay);
    }

    auto obj_dv =
        corm_obj_new(PA_CLASS_(P_DMM(MeasurementresultDelayvariation)),
                     key.data(),
                     key.size());
    if (obj_dv)
    {
        objs[n_objs++] = obj_dv;

        rc |= corm_uint32_set( // FIELD__ifdv_two_way_avg /*uint32_t*/
            obj_dv,
            PA_FIELD_(P_DMM(MeasurementresultDelayvariation), ifdv_two_way_avg),
            stats.jitter);
        rc |= corm_uint32_set( // FIELD__ifdv_two_way_max /*uint32_t*/
            obj_dv,
            PA_FIELD_(P_DMM(MeasurementresultDelayvariation), ifdv_two_way_max),
            stats.delay_variation_max);
        rc |= corm_uint32_set( // FIELD__last_jitter /*uint32_t*/
            obj_dv,
            PA_FIELD_(P_DMM(MeasurementresultDelayvariation), last_jitter),
            stats.jitter);
    }

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed corm_set for session %s result index %d - %d objects",
                proa.name.c_str(),
                proa.result_index,
                n_objs);
        corm_obj_destroy_array(objs.data(), n_objs);
    }
    else if (auto sent = dbclient_set(
                 objs.data(), n_objs, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             n_objs != sent)
    {
        CFM_LOG(DN_LOG_ERR,
                "dbclient_set session %s result index %d - %d objects",
                proa.name.c_str(),
                proa.result_index,
                n_objs);
    }
}

void proactiveResultInvalid(const Proactive& proa) noexcept
{
    const auto idx = fmt::format("{}", proa.result_index);
    std::array<const char*, 2> key = {
        proa.name.c_str(),
        idx.c_str(),
    };

    auto obj = corm_obj_new(
        PA_CLASS_(P_DMM(MeasurementresultItem)), key.data(), key.size());

    if (not obj)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate DMM Measurementresult corm_obj for "
                "session %s result index %d",
                proa.name.c_str(),
                proa.result_index);
        return;
    }

    int rc = corm_enum_set( // FIELD__measurement_validity
        obj,
        PA_FIELD_(P_DMM(MeasurementresultItem), measurement_validity),
        PA_FIELD_(P_DMM(MeasurementresultMeasurementvalidityEnum), invalid));

    const std::time_t tm = time(NULL);
    char tbuf[32];
    // ISO 8601
    strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

    // FIELD__end_time
    rc |= corm_string_set(
        obj, PA_FIELD_(P_DMM(MeasurementresultItem), end_time), tbuf);

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed corm_set for session %s result index %d",
                proa.name.c_str(),
                proa.result_index);
        corm_obj_destroy(obj);
    }
    else if (auto sent =
                 dbclient_set(&obj, 1, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             not sent)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed dbclient_set for session %s result index %d",
                proa.name.c_str(),
                proa.result_index);
    }
}

} // namespace cfm
