#include "CfmSession.hpp"

#include <spdlog/fmt/fmt.h>

#include "cfm_manager/cfm_initiator.h"
#include "CfmInitiator.hpp"
#include "CfmLocalMep.hpp"
#include "CfmManager.hpp"
#include "operational/include/corm_api.h"
#include "orm/class_enum.h"

namespace cfm {

CfmSession::CfmSession(uint64_t sess_id,
                       stw_t* timer_wheel,
                       const SessionStartRequest& req,
                       const LMepOper& lmep_op)
    : sess_id(sess_id), cfg(req, lmep_op), m_timer_wheel(timer_wheel)
{
    stw_timer_prepare(&m_timer);
    CfmInitiator::sessions_allocated++;
}

CfmSession::~CfmSession() { CfmInitiator::sessions_freed++; }

bool CfmSession::IsSupported() { return true; }

void CfmSession::HandlePacket([[maybe_unused]] wb_pkt* pkt)
{ //DMM doesn't implement this
}

int CfmSession::IncRefcount() { return refcount++; }

int CfmSession::DecRefcount() { return refcount--; }

int CfmSession::GetRefcount() { return refcount; }

CfmSession::Config::Config(const SessionStartRequest& req,
                           const LMepOper& lmep_op) noexcept
    : pkt_count(req.pkt_count), pkt_interval_ms(req.interval_ms),
      pkt_size(req.frame_size), pcp(req.pcp), oam_id(req.oam_id),
      hw_id(lmep_op.oam_info.hw_id), mep_id(lmep_op.config.mep_id),
      outer_tag(lmep_op.config.outer_tag),
      outer_tpid(lmep_op.config.outer_tpid),
      inner_tag(lmep_op.config.inner_tag),
      inner_tpid(lmep_op.config.inner_tpid),
      direction(lmep_op.config.direction), level(lmep_op.config.md_level),
      proactive(req.proactive)
{
    memcpy(&src_mac, lmep_op.src_mac_address.data(), sizeof(src_mac));

    const ether_addr* req_dmac = &req.dmac;
    if (req.rmep_id)
    {
        auto rmep_it = lmep_op.rmep_db.find(*req.rmep_id);
        if (rmep_it != lmep_op.rmep_db.end())
        {
            const auto& rmep = rmep_it->second;
            req_dmac = &rmep.status.mac_address;
        }
    }
    memcpy(&dst_mac, req_dmac, sizeof(dst_mac));
}

static inline auto invalid(SessionOpcode op) noexcept
    -> std::pair<field_id, field_id>
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return {
            OD_FIELD_(DMM(Testresults), measurement_validity),
            OD_FIELD_(DMM(TestresultsMeasurementvalidityEnum), invalid),
        };
    case SessionOpcode::SESS_SLM:
        return {
            OD_FIELD_(SLM(Testresults), measurement_validity),
            OD_FIELD_(SLM(TestresultsMeasurementvalidityEnum), invalid),
        };
    case SessionOpcode::SESS_LTM:
        return {
            OD_FIELD_(LTM(Testresults), measurement_validity),
            OD_FIELD_(LTM(TestresultsMeasurementvalidityEnum), invalid),
        };
    case SessionOpcode::SESS_LBM:
        return {
            OD_FIELD_(LBM(Testresults), measurement_validity),
            OD_FIELD_(LBM(TestresultsMeasurementvalidityEnum), invalid),
        };
    default:
        __builtin_unreachable();
    }
}

static inline class_id od_testresults_class(SessionOpcode op)
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return OD_CLASS_(DMM(Testresults));
    case SessionOpcode::SESS_SLM:
        return OD_CLASS_(SLM(Testresults));
    case SessionOpcode::SESS_LTM:
        return OD_CLASS_(LTM(Testresults));
    case SessionOpcode::SESS_LBM:
        return OD_CLASS_(LBM(Testresults));
    default:
        __builtin_unreachable();
    }
}

static inline class_id od_testinfo_class(SessionOpcode op)
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return OD_CLASS_(DMM(Testinfo));
    case SessionOpcode::SESS_SLM:
        return OD_CLASS_(SLM(Testinfo));
    case SessionOpcode::SESS_LTM:
        return OD_CLASS_(LTM(Testinfo));
    case SessionOpcode::SESS_LBM:
        return OD_CLASS_(LBM(Testinfo));
    default:
        __builtin_unreachable();
    }
}

static inline field_id od_end_time_field(SessionOpcode op)
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return OD_FIELD_(DMM(Testinfo), end_time);
    case SessionOpcode::SESS_SLM:
        return OD_FIELD_(SLM(Testinfo), end_time);
    case SessionOpcode::SESS_LTM:
        return OD_FIELD_(LTM(Testinfo), end_time);
    case SessionOpcode::SESS_LBM:
        return OD_FIELD_(LBM(Testinfo), end_time);
    default:
        __builtin_unreachable();
    }
}

void CfmSession::OperTestresultInvalid(SessionOpcode type) noexcept
{
    const auto& lmeps = CfmManager::GetInstance().GetOperation().data.Meps;

    auto lmep_it = lmeps.find(cfg.oam_id);
    if (lmeps.end() == lmep_it) return;

    const auto& lmep = lmep_it->second.config;

    const auto mep_id_str = fmt::format("{}", lmep.mep_id);
    std::array<const char*, 3> lmep_key = {
        lmep.md_id.c_str(),
        lmep.ma_id.c_str(),
        mep_id_str.c_str(),
    };

    CFM_LOG(DN_LOG_INFO,
            "%s Testresults 'invalid' %lx session - MD %s MA %s LMEP %d",
            sess_get_opcode_str(type),
            sess_id,
            lmep.md_id.c_str(),
            lmep.ma_id.c_str(),
            lmep.mep_id);

    auto obj_r = corm_obj_new(
        od_testresults_class(type), lmep_key.data(), lmep_key.size());
    if (not obj_r)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate Testresults corm_obj for session id %lx",
                sess_id);
        return;
    }

    auto obj_i =
        corm_obj_new(od_testinfo_class(type), lmep_key.data(), lmep_key.size());
    if (not obj_i)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate Testinfo corm_obj for session id %lx",
                sess_id);
        return;
    }

    std::array<corm_obj*, 2> objs = {obj_i, obj_r};

    const auto [v_field, v_value] = invalid(type);
    // FIELD__Testresults__measurement_validity
    int rc = corm_enum_set(obj_r, v_field, v_value);

    if (obj_i)
    {
        const std::time_t tm = time(NULL);
        char tbuf[32];
        // ISO 8601
        strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

        // FIELD__end_time
        rc |= corm_string_set(obj_i, od_end_time_field(type), tbuf);
    }

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR, "Failed corm_set for session id %lx", sess_id);
        corm_obj_destroy(obj_r);
        corm_obj_destroy(obj_i);
    }
    else if (auto sent = dbclient_set(
                 objs.data(), objs.size(), E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             not sent)
    {
        CFM_LOG(DN_LOG_ERR, "Failed dbclient_set for session id %lx", sess_id);
    }
}

} // namespace cfm
