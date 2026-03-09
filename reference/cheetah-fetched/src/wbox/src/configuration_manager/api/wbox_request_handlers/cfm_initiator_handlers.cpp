#include "cfm_initiator_handlers.hpp"

#include "app.h"
#include "dn_logger/include/dn_logger.h"
#include "libdatapath/transactions/transaction_manager.h"
#include "sdk_common/sdk_wrap_cfm.h"
#include "wb_api/cfm_initiator.pb.h"
#include "wbox/src/cfm_manager/CfmManager.hpp"
#include "wbox/src/cfm_manager/CfmSessionLTM.hpp"
#include "wbox/src/cfm_manager/CfmSessionStats.hpp"
#include "wbox/src/cfm_manager/CfmTypes.hpp"
#include "wbox/src/cfm_manager/cfm_initiator.h"

extern "C"
{
#include "libdatapath/general.h"
}

#pragma GCC diagnostic ignored "-Wunused-parameter"

#include <ranges>

using namespace cfm;

static SessionOpcode
get_sess_opcode(wb_api::cfm_initiator::SessionOpcode type)
{
	switch (type)
	{
		case wb_api::cfm_initiator::SessionOpcode::DMM:
			return SESS_DMM;
		case wb_api::cfm_initiator::SessionOpcode::SLM:
			return SESS_SLM;
		case wb_api::cfm_initiator::SessionOpcode::LBM:
			return SESS_LBM;
		case wb_api::cfm_initiator::SessionOpcode::LTM:
			return SESS_LTM;
		default:
			return SESS_UNKNOWN;
	}
}

static wb_api::cfm_initiator::SessionStartStatus
get_sess_start_status(SessionStartStatus status)
{
	switch (status)
	{
		case SESS_START_OK:
			return wb_api::cfm_initiator::SessionStartStatus::START_OK;
		case SESS_START_ERR_EXISTS:
			return wb_api::cfm_initiator::SessionStartStatus::START_ERR_EXISTS;
		case SESS_START_ERR_MISSING_MEP:
			return wb_api::cfm_initiator::SessionStartStatus::START_ERR_MISSING_MEP;
		case SESS_START_ERR_DISABLED_MEP:
			return wb_api::cfm_initiator::SessionStartStatus::START_ERR_DISABLED_MEP;
		case SESS_START_ERR_MISSING_MAC:
			return wb_api::cfm_initiator::SessionStartStatus::START_ERR_MISSING_MAC;
		case SESS_START_ERR_COMMIT_PROGRESS:
			return wb_api::cfm_initiator::SessionStartStatus::START_ERR_COMMIT_PROGRESS;
		case SESS_START_ERR_UNSUPPORTED:
			return wb_api::cfm_initiator::SessionStartStatus::START_ERR_UNSUPPORTED;
		case SESS_START_ERR:
			return wb_api::cfm_initiator::SessionStartStatus::START_ERR_GENERIC;
		default:
			CFM_LOG(DN_LOG_ERR, "INITIATOR: unknown start status");
			return wb_api::cfm_initiator::SessionStartStatus::START_ERR_GENERIC;
	}
}

static wb_api::cfm_initiator::SessionStopStatus
get_sess_stop_status(SessionStopStatus status)
{
	switch (status)
	{
		case SESS_STOP_OK:
			return wb_api::cfm_initiator::SessionStopStatus::STOP_OK;
		case SESS_STOP_ERR:
			return wb_api::cfm_initiator::SessionStopStatus::STOP_ERR;
		default:
			CFM_LOG(DN_LOG_ERR, "INITIATOR: unknown stop status");
			return wb_api::cfm_initiator::SessionStopStatus::STOP_ERR;
	}
}

class CfmInitiatorStartRequestHandler : public CfmInitiatorRequestHandler<GetterRequestHandler>
{
protected:
	int
	handleCfmInitiatorRequest(struct wbox_api_context &ctx,
				  const wb_api::cfm_initiator::ApiRequest &request,
				  wb_api::cfm_initiator::ApiResponse &response)
	{
		const auto &cfm_start = request.start_req();

		auto &cfm_mgr = CfmManager::GetInstance();
		SessionStartRequest start_sess{};
		start_sess.type = get_sess_opcode(cfm_start.sess_type());
		start_sess.oam_id = cfm_start.source().mep_id();

		const auto &dst = cfm_start.dest();

		if (dst.has_rmep_id())
			start_sess.rmep_id = dst.rmep_id();
		else if (dst.has_dmac()) // MAC string is validated in CLI
			sscanf(dst.dmac().c_str(),
			       "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx",
			       &start_sess.dmac.addr_bytes[0],
			       &start_sess.dmac.addr_bytes[1],
			       &start_sess.dmac.addr_bytes[2],
			       &start_sess.dmac.addr_bytes[3],
			       &start_sess.dmac.addr_bytes[4],
			       &start_sess.dmac.addr_bytes[5]);

		if (!(cfm_start.has_interval_ms() && cfm_start.has_pkt_count()) &&
		    start_sess.type != SessionOpcode::SESS_LTM)
		{
			CFM_LOG(DN_LOG_ERR,
				"INITIATOR START: request missing `interval` or `pkt_count`");
			return -1;
		}

		if (cfm_start.has_interval_ms())
			start_sess.interval_ms = cfm_start.interval_ms();
		if (cfm_start.has_pkt_count())
			start_sess.pkt_count = cfm_start.pkt_count();
		if (cfm_start.has_frame_size())
			start_sess.frame_size = cfm_start.frame_size();
		if (cfm_start.has_pcp())
			start_sess.pcp = cfm_start.pcp();
		if (cfm_start.has_max_hops())
			start_sess.max_hops = cfm_start.max_hops();

		auto &initiator = cfm_mgr.GetInitiator();

		SessionStartResponse ret = initiator.CreateSession(start_sess);
		if (ret.status == SESS_START_OK) // Enqueue event for CfgManager thread
			initiator.SendStartSessionEvent(ret.sess_id);

		auto start_response = response.mutable_start_resp();
		start_response->set_id(ret.sess_id);
		start_response->set_status(get_sess_start_status(ret.status));

		CFM_LOG(DN_LOG_INFO,
			"INITIATOR START: %s sess_id %lx status %d",
			sess_get_opcode_str(start_sess.type),
			ret.sess_id,
			ret.status);

		return 0;
	}
};

class CfmInitiatorStopRequestHandler : public CfmInitiatorRequestHandler<GetterRequestHandler>
{
protected:
	int
	handleCfmInitiatorRequest(struct wbox_api_context &ctx,
				  const wb_api::cfm_initiator::ApiRequest &request,
				  wb_api::cfm_initiator::ApiResponse &response)
	{
		const auto &cfm_stop = request.stop_req();

		auto &cfm_mgr = CfmManager::GetInstance();
		SessionStopRequest stop_sess{};
		stop_sess.sess_id = cfm_stop.id();

		SessionStopResponse ret = cfm_mgr.GetInitiator().TerminateSession(stop_sess);

		auto stop_response = response.mutable_stop_resp();
		stop_response->set_id(ret.sess_id);
		stop_response->set_status(get_sess_stop_status(ret.status));

		CFM_LOG(DN_LOG_INFO,
			"INITIATOR STOP: sess_id %lx status %d",
			ret.sess_id,
			ret.status);

		return 0;
	}
};

class CfmInitiatorProactiveAdd : public CfmInitiatorRequestHandler<GetterRequestHandler>
{
	int
	handleCfmInitiatorRequest(struct wbox_api_context &ctx,
				  const wb_api::cfm_initiator::ApiRequest &request,
				  wb_api::cfm_initiator::ApiResponse &response)
	{
		const auto &req = request.proactive_add();
		auto &intr = CfmManager::GetInstance().GetInitiator();

		intr.pro_sched.AddSession(req);

		return 0;
	}
};

class CfmInitiatorProactiveDel : public CfmInitiatorRequestHandler<GetterRequestHandler>
{
	int
	handleCfmInitiatorRequest(struct wbox_api_context &ctx,
				  const wb_api::cfm_initiator::ApiRequest &request,
				  wb_api::cfm_initiator::ApiResponse &response)
	{
		const auto &req = request.proactive_del();
		auto &intr = CfmManager::GetInstance().GetInitiator();

		intr.pro_sched.DelSession(req);

		return 0;
	}
};

CfmInitiatorHandlersMap::CfmInitiatorHandlersMap()
{
	m_handlers_map[wb_api::cfm_initiator::ApiRequest::kStartReq] =
		std::make_shared<CfmInitiatorStartRequestHandler>();
	m_handlers_map[wb_api::cfm_initiator::ApiRequest::kStopReq] =
		std::make_shared<CfmInitiatorStopRequestHandler>();
	m_handlers_map[wb_api::cfm_initiator::ApiRequest::kProactiveAdd] =
		std::make_shared<CfmInitiatorProactiveAdd>();
	m_handlers_map[wb_api::cfm_initiator::ApiRequest::kProactiveDel] =
		std::make_shared<CfmInitiatorProactiveDel>();
}
