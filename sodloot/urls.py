from django.urls import path

from checker import views

urlpatterns = [
    path("", views.index, name="index"),
    path("leaderboard", views.leaderboard, name="leaderboard"),
    path("reports", views.reports, name="reports"),
    path("softres", views.softres_audit, name="softres"),
    path("contested", views.contested, name="contested"),
    path("comp", views.comp_builder, name="comp"),
    path("api/eligibility", views.eligibility, name="eligibility"),
    path("api/topdps", views.top_dps, name="top_dps"),
    path("api/leaderboard", views.api_leaderboard, name="api_leaderboard"),
    path("api/reportcard", views.api_report_card, name="api_report_card"),
    path("api/softres", views.api_softres, name="api_softres"),
    path("api/contested", views.api_contested, name="api_contested"),
    path("api/comp", views.api_comp, name="api_comp"),
    path("api/comp/build", views.api_comp_build, name="api_comp_build"),
    path("api/comp/rules", views.api_comp_rules, name="api_comp_rules"),
    path("api/comp/warnings", views.api_comp_warnings, name="api_comp_warnings"),
    path("api/comp/save", views.api_comp_save, name="api_comp_save"),
    path("api/comp/suggestions", views.api_comp_suggestions, name="api_comp_suggestions"),
    path("api/atiesh", views.api_atiesh, name="api_atiesh"),
    path("api/atiesh/set", views.api_atiesh_set, name="api_atiesh_set"),
    path("api/items", views.item_search, name="item_search"),
    path("api/characters", views.character_search, name="character_search"),
]
